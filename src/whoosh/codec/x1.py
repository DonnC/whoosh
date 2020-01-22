# Copyright 2012 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

"""
This module implements a "codec" for writing/reading Whoosh X indexes.
"""

import re
import logging
import struct
import typing
from collections import defaultdict
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Set, Tuple, Union, cast)

from whoosh import columns, fields, reading, storage
from whoosh.matching import matchers
from whoosh.codec import codecs
from whoosh.filedb import blueline, filestore
from whoosh.filedb.datafile import Data, OutputFile
from whoosh.metadata import MetaData
from whoosh.postings import basic, postform, postings, ptuples
from whoosh.system import IS_LITTLE
from whoosh.util import now, readable_tens

# Typing imports
if typing.TYPE_CHECKING:
    from whoosh import scoring

try:
    import zlib
except ImportError:
    zlib = None


logger = logging.getLogger(__name__)


# Typing aliases

TermTuple = Tuple[str, bytes]


# Struct for coding field names as bytes in the terms file
fieldnum_struct = struct.Struct(">H")

# Magic number at start of postings file
POSTINGS_MAGIC = b"X1Co"

# Column type to store field length info
LENGTHS_COLUMN = columns.CompactIntColumn(default=0)
# Column type to store pointers to encoded vectors
VECTOR_COLUMN = columns.CompressedBytesColumn()
# Column type to store values of stored fields
STORED_COLUMN = columns.PickleColumn(columns.CompressedBytesColumn())


# Patterns and functions for generating and matching file names

segfile_regex = re.compile("""
x1_  # Identifies this file as having been written by this codec
(?P<segid>[A-Za-z0-9]+)  # Segment ID
_(?P<name>[A-Za-z]*)  # Field name (may be blank for general files)
(?P<ext>[.][A-Za-z]+)  # Extension, indicates type of data in file
""", re.VERBOSE | re.UNICODE)


# Header for terms file

class TermsFileHeader(MetaData):
    magic_bytes = b"X1Th"
    flags = "was_little"

    was_little = IS_LITTLE


class TermsFileFooter(MetaData):
    magic_bytes = b"X1Tf"
    flags = "was_little"
    field_order = "refs_offset refs_count names_offset names_count"

    was_little = IS_LITTLE

    refs_offset = "q"
    refs_count = "i"
    names_offset = "q"
    names_count = "H"


class PostFileHeader(MetaData):
    magic_bytes = b"X1Po"
    flags = "was_little"

    was_little = IS_LITTLE


# Functions to generate fake field names for internal columns

def _vecfield(fieldname: str) -> str:
    return "_%s_vec" % fieldname


def _lenfield(fieldname: str) -> str:
    return "_%s_len" % fieldname


# Term info implementation

class X1TermInfo(reading.TermInfo):
    # B   | Flags
    # f   | Total weight
    # I   | Total doc freq
    # i   | Min length
    # i   | Max length
    # f   | Max weight
    # I   | Minimum (first) ID
    # I   | Maximum (last) ID
    # i   | Inline bytes length OR block count
    header = struct.Struct("<BfIiifIIi")

    # Posting data offset
    postingref = struct.Struct("<q")

    def __init__(self, weight: float=0, df: int=0, minlength: int=None,
                 maxlength: int=0, maxweight: float=0, minid: int=None,
                 maxid: int=None, offset: int=-1, inlinebytes: bytes=None):
        super(X1TermInfo, self).__init__(
            weight=weight, df=df, minlength=minlength, maxlength=maxlength,
            maxweight=maxweight, minid=minid, maxid=maxid
        )

        # Offset into postings file
        self.offset = offset
        # Number of posting blocks
        self.blockcount = 0

        # It's possible to "inline" a short (usually length 1) posting list
        # in the term info instead of writing it to the posting file separately
        self.inlinebytes = inlinebytes

    def __repr__(self):
        return ("<%s weight=%s df=%s minlen=%s maxlen=%s maxw=%s minid=%s "
                "maxid=%s, offset=%d, blockcount=%d>" %
                (type(self).__name__, self._weight, self._df, self._minlength,
                 self._maxlength, self._maxweight, self._minid, self._maxid,
                 self.offset, self.blockcount))

    def add_block(self, posts: Sequence[ptuples.PostTuple]):
        self.blockcount += 1
        self.add_posting_list_stats(posts)

    def has_blocks(self) -> bool:
        if self.inlinebytes is not None:
            return False
        return bool(self.blockcount)

    def to_bytes(self) -> bytes:
        inlinebytes = self.inlinebytes
        isinlined = inlinebytes is not None

        flags = (
            isinlined << 0
        )

        if isinlined:
            length = len(inlinebytes)
        else:
            length = self.blockcount

        minlength = 0 if self._minlength is None else self._minlength
        assert self._minid is not None
        assert self._maxid is not None

        out = bytearray()
        # Pack the term info into the header
        out += self.header.pack(flags, self._weight, self._df,
                                minlength, self._maxlength, self._maxweight,
                                self._minid, self._maxid, length)

        if isinlined:
            # Postings are inlined - encode them and add them to the header
            out += inlinebytes
        else:
            # Postings are external - add the offset to the posting data
            out += self.postingref.pack(self.offset)

        return out

    @classmethod
    def _unpack_header(cls, data: Union[bytes, Data], offset: int) -> Tuple:
        return cls.header.unpack(data[offset:offset + cls.header.size])

    @classmethod
    def from_bytes(cls, bs: bytes, offset: int=0) -> 'X1TermInfo':
        ti = cls()

        # Pull information out of the header
        (flags, ti._weight, ti._df, ti._minlength, ti._maxlength, ti._maxweight,
         ti._minid, ti._maxid, length) = cls._unpack_header(bs, offset)

        pos = offset + cls.header.size
        if flags & 1:
            # The last field in the header is the number of inlined bytes
            # Postings are stored inline after the header
            ti.inlinebytes = bytes(bs[pos:pos + length])
        else:
            # The last field in the header is the number of posting blocks
            ti.blockcount = length
            # Bytes after the header are pointer into posting file
            postref = cls.postingref
            ti.offset = postref.unpack(bs[pos:pos + postref.size])[0]

        return ti

    def copy_from(self, terminfo: 'X1TermInfo',
                  docmap_get: Callable[[int, int], int]=None):
        super(X1TermInfo, self).copy_from(terminfo, docmap_get)
        self.offset = terminfo.offset
        self.inlinebytes = terminfo.inlinebytes
        self.blockcount = terminfo.blockcount

    # Methods to efficiently pull info off disk without instantiating the whole
    # TermInfo object

    @classmethod
    def decode_weight(cls, data: Union[bytes, Data], offset: int) -> float:
        return cls._unpack_header(data, offset)[1]

    @classmethod
    def decode_doc_freq(cls, data: Union[bytes, Data], offset: int) -> float:
        return cls._unpack_header(data, offset)[2]

    @classmethod
    def decode_min_and_max_length(cls, data: Union[bytes, Data],
                                  offset: int) -> Tuple[int, int]:
        vals = cls._unpack_header(data, offset)
        return vals[3], vals[4]

    @classmethod
    def decode_max_weight(cls, data: Union[bytes, Data], offset: int) -> float:
        return cls._unpack_header(data, offset)[5]


# Segment implementation

class X1Segment(codecs.FileSegment):
    def __init__(self, _codec: 'X1Codec', indexname: str, segid: str,
                 doccount: int=0, deleted: Set=None, fieldlengths: Dict=None,
                 was_little: bool=IS_LITTLE):
        self._codec = _codec
        self._indexname = indexname
        self._segid = segid

        self._size = 0
        self._doccount = doccount
        self._deleted = deleted

        self.fieldlengths = fieldlengths or {}
        self.was_little = was_little
        self.is_compound = False
        self.compound_filename = None

    def __repr__(self):
        return "<{} {} {}/{} {}>".format(
            type(self).__name__, self.segment_id(), self.doc_count(),
            self.doc_count_all(), readable_tens(self.size())
        )

    def make_filename(self, ext: str, subname: str=None) -> str:
        from whoosh import index
        return index.make_segment_filename(self.index_name(), self.segment_id(),
                                           ext, subname)

    def make_col_filename(self, fieldname: str) -> str:
        return self.make_filename("%s.col" % fieldname)

    def native(self) -> bool:
        return IS_LITTLE == self.was_little

    def codec(self) -> codecs.Codec:
        return self._codec

    def size(self) -> int:
        return self._size

    def set_size(self, size: int):
        self._size = size

    def add_size(self, size: int):
        self._size += size

    def set_doc_count(self, dc: int):
        self._doccount = dc

    def doc_count_all(self) -> int:
        return self._doccount

    def deleted_count(self) -> int:
        if self._deleted is None:
            return 0
        return len(self._deleted)

    def deleted_docs(self) -> Iterable[int]:
        if self._deleted is None:
            return ()
        else:
            return iter(self._deleted)

    def delete_document(self, docnum: int, delete: bool=True):
        if delete:
            if self._deleted is None:
                self._deleted = set()
            self._deleted.add(docnum)
        elif self._deleted is not None and docnum in self._deleted:
            self._deleted.clear(docnum)

    def is_deleted(self, docnum: int) -> bool:
        if self._deleted is None:
            return False
        return docnum in self._deleted

    def field_length(self, fieldname: str, default: int=0):
        return self.fieldlengths.get(fieldname, default)


# Codec

class X1Codec(codecs.Codec):
    # File extensions
    TERMS_EXT = "trm"  # Term index
    POSTS_EXT = "pst"  # Term postings
    VPOSTS_EXT = "vps"  # Vector postings
    COLUMN_EXT = "col"  # Per-document value columns
    SEGMENT_EXT = "seg"  # Compound segment

    def __init__(self, blocklimit: int=128, compression: int=3,
                 inlinelimit: int=1, assemble: bool=False):
        self._blocklimit = blocklimit
        self._compression = compression
        self._inlinelimit = inlinelimit
        self._assemble = assemble
        self._io = basic.BasicIO()

    @classmethod
    def from_json(cls, data: dict) -> 'X1Codec':
        kwargs = {}
        for key in ("blocklimit", "compression", "inlinelimit", "assemble"):
            if key in data:
                kwargs[key] = data[key]
        return cls(**kwargs)

    def as_json(self) -> dict:
        return {"class": "%s.%s" % (self.__module__, type(self).__name__),
                "blocklimit": self._blocklimit,
                "compression": self._compression,
                "inlinelimit": self._inlinelimit,
                "assemble": self._assemble}

    # Self

    def name(self) -> str:
        return "whoosh.codec.x1.X1Codec"

    def short_name(self) -> str:
        return "x1"

    # def automata(self):

    # Per-document value writer
    def per_document_writer(self, session: 'storage.Session',
                            segment: X1Segment) -> 'X1PerDocWriter':
        return X1PerDocWriter(session, segment)

    # Inverted index writer
    def field_writer(self, session: 'storage.Session',
                     segment: X1Segment, subname: str=None) -> 'X1FieldWriter':
        return X1FieldWriter(session, segment, subname=subname)

    # automata

    # Readers
    def per_document_reader(self, session: 'storage.Session',
                            segment: X1Segment) -> 'X1PerDocReader':
        return X1PerDocReader(session, segment)

    def terms_reader(self, session: 'storage.Session',
                     segment: X1Segment, subname: str=None) -> 'X1TermsReader':
        return X1TermsReader(session, segment, subname=subname)

    # Segments

    def new_segment(self, session: 'storage.Session') -> X1Segment:
        segid = "%06d" % session.next_id()
        return X1Segment(self, session.indexname, segid)

    def finish_segment(self, session: 'storage.Session', segment: X1Segment):
        from whoosh.filedb.compound import assemble_segment

        store = session.store
        filename = segment.make_filename(X1Codec.SEGMENT_EXT)

        assemble_segment(store, store, segment, filename, delete=True)

        segment.is_compound = True
        segment.compound_filename = filename

    def segment_storage(self, store: 'filestore.FileStorage',
                        segment: X1Segment) -> 'filestore.FileStorage':
        if segment.is_compound:
            from whoosh.filedb.compound import CompoundStorage

            return CompoundStorage(store, segment.compound_filename)

        return store

    def segment_from_bytes(self, bs:bytes) -> X1Segment:
        return cast(X1Segment, X1Segment.from_bytes(bs))

    def postings_io(self) -> 'postings.PostingsIO':
        return self._io


# Per-doc

class X1PerDocWriter(codecs.PerDocumentWriter):
    def __init__(self, session: 'storage.Session', segment: X1Segment):
        self._store = session.store
        self._segment = segment
        self._io = segment.codec().postings_io()

        self._segid = segment.segment_id()

        # Cached column writers map fieldname -> (OutputFile, colwriter)
        self._cws = {}  # type: Dict[str, Tuple[OutputFile, columns.ColumnWriter]]

        self._fieldlengths = defaultdict(int)
        self._docnum = -1
        self._storedfields = None
        self._indoc = False
        self.closed = False

    def _colwriter(self, fieldname: str, column: 'columns.Column'
                   ) -> 'columns.ColumnWriter':
        # Return a column writer for the given field
        _cws = self._cws
        if fieldname in _cws:
            cw = _cws[fieldname][1]
        else:
            filename = self._segment.make_col_filename(fieldname)
            f = self._store.create_file(filename)
            cw = column.writer(f)
            _cws[fieldname] = f, cw

        return cw

    def postings_io(self) -> 'postings.PostingsIO':
        return self._io

    def start_doc(self, docnum: int):
        self._docnum += 1
        if self._indoc:
            raise Exception("Called start_doc when already in a doc")
        if docnum != self._docnum:
            raise Exception("Called start_doc(%r) was expecting %r"
                            % (docnum, self._docnum))
        self._storedfields = {}
        self._indoc = True

    def add_field(self, fieldname: str, fieldobj: 'fields.FieldType',
                  value: Any, length: int):

        if fieldobj.stored and value is not None:
            self._storedfields[fieldname] = value

        if fieldobj.store_lengths and length:
            # Add byte to length column
            self.add_column_value(_lenfield(fieldname), LENGTHS_COLUMN, length)
            self._fieldlengths[fieldname] += length

    def add_column_value(self, fieldname: str, column: 'columns.Column',
                         value: Any):
        cw = self._colwriter(fieldname, column)
        cw.add(self._docnum, value)

    def add_vector_postings(self, fieldname: str, fieldobj: 'fields.FieldType',
                            posts: 'Sequence[postings.PostTuple]'):
        data = self._io.vector_to_bytes(fieldobj.vector, posts)
        self.add_raw_vector(fieldname, data)

    def add_raw_vector(self, fieldname: str, data: bytes):
        self.add_column_value(_vecfield(fieldname), VECTOR_COLUMN, data)

    def finish_doc(self):
        if not self._indoc:
            raise Exception("Called finish outside a document")

        sf = self._storedfields
        if sf:
            # Add the stored fields to the stored fields column
            self.add_column_value("_stored", STORED_COLUMN, sf)
            sf.clear()
        self._indoc = False

    def close(self):
        if self._indoc:
            # Called close without calling finish_doc
            self.finish_doc()
        totaldocs = self._docnum + 1

        # Store the number of documents in the segment
        self._segment.set_doc_count(totaldocs)
        # Store the overall field lengths in the segment
        self._segment.fieldlengths = self._fieldlengths

        # Finish open columns and close their files
        perdocsize = 0
        for key, (colfile, colwriter) in self._cws.items():
            colwriter.finish(totaldocs)
            perdocsize += colfile.tell()
            colfile.close()

        # Add the total size of all the columns to the segment size
        self._segment.add_size(perdocsize)

        self.closed = True


class X1PerDocReader(codecs.PerDocumentReader):
    def __init__(self, session: 'storage.Session', segment: X1Segment):
        self._store = session.store
        self._segment = segment
        self._io = segment.codec().postings_io()

        self._segid = segment.segment_id()
        self._doccount = segment.doc_count_all()

        # Cache open column files
        self._colfiles = {}  # type: Dict[str, Tuple[Data, int, int, str]]
        # Cache column readers
        self._colreaders = {}  # type: Dict[str, columns.ColumnReader]

        # Cache per-field min lengths and max lengths
        self._minlengths = {}  # type: Dict[str, int]
        self._maxlengths = {}  # type: Dict[str, int]

        self.closed = False

    def close(self):
        for colreader in self._colreaders.values():
            colreader.close()

        for colfile, _, _, filename in self._colfiles.values():
            try:
                colfile.close()
            except BufferError:
                raise BufferError("Buffer error closing %s" % filename)
        self.closed = True

    def doc_count(self) -> int:
        return self._doccount - self._segment.deleted_count()

    def doc_count_all(self) -> int:
        return self._doccount

    def all_doc_ids(self) -> Iterable[int]:
        is_deleted = self._segment.is_deleted
        return (docnum for docnum in range(self._doccount)
                if not is_deleted(docnum))

    # Deletions

    def has_deletions(self) -> bool:
        return self._segment.has_deletions()

    def is_deleted(self, docnum: int) -> bool:
        return self._segment.is_deleted(docnum)

    def deleted_docs(self) -> Iterable[int]:
        return self._segment.deleted_docs()

    # Columns

    def supports_columns(self):
        return True

    def has_column(self, fieldname: str) -> bool:
        filename = self._segment.make_col_filename(fieldname)
        return self._store.file_exists(filename)

    def column_reader(self, fieldname: str, column: columns.Column,
                      reverse: bool=False) -> columns.ColumnReader:
        _colfiles = self._colfiles
        if fieldname in _colfiles:
            colfile, offset, length, _ = _colfiles[fieldname]
        else:
            filename = self._segment.make_col_filename(fieldname)
            length = self._store.file_length(filename)
            colfile = self._store.map_file(filename)
            offset = 0
            _colfiles[fieldname] = colfile, offset, length, filename

        return column.reader(colfile, offset, length, self._doccount,
                             native=self._segment.native(), reverse=reverse)

    # Lengths

    def _cached_reader(self, fieldname, column
                       ) -> 'Optional[columns.ColumnReader]':
        # Caches and retrieves commonly used column readers such as the lengths

        if fieldname in self._colreaders:
            return self._colreaders[fieldname]
        else:
            if not self.has_column(fieldname):
                return None

            reader = self.column_reader(fieldname, column)
            assert reader is not None
            self._colreaders[fieldname] = reader
            return reader

    def doc_field_length(self, docnum: int, fieldname: str, default: int=0
                         ) -> int:
        assert isinstance(docnum, int)
        if docnum > self._doccount:
            raise IndexError("Asked for docnum %r of %d"
                             % (docnum, self._doccount))

        reader = self._cached_reader(_lenfield(fieldname), LENGTHS_COLUMN)
        if reader is None:
            return default
        return reader[docnum]

    def field_length(self, fieldname: str) -> int:
        return self._segment.field_length(fieldname, 0)

    def _minmax_length(self, fieldname, op, cache):
        # Compute the minimum or maximum field length across the segment, and
        # cache the results

        if fieldname in cache:
            return cache[fieldname]

        lenfield = _lenfield(fieldname)
        reader = self._cached_reader(lenfield, LENGTHS_COLUMN)
        length = op(reader)
        cache[fieldname] = length
        return length

    def min_field_length(self, fieldname: str) -> int:
        return self._minmax_length(fieldname, min, self._minlengths)

    def max_field_length(self, fieldname: str) -> int:
        return self._minmax_length(fieldname, max, self._maxlengths)

    # Vectors

    def _vector_bytes(self, docnum: int, fieldname: str) -> Optional[bytes]:
        if docnum > self._doccount:
            raise IndexError("Asked for document %r of %d"
                             % (docnum, self._doccount))

        vecfield = _vecfield(fieldname)
        vreader = self._cached_reader(vecfield, VECTOR_COLUMN)
        if vreader:
            return vreader[docnum]

    def has_vector(self, docnum: int, fieldname: str):
        return bool(self._vector_bytes(docnum, fieldname))

    def vector(self, docnum: int, fieldname: str):
        vbytes = self._vector_bytes(docnum, fieldname)
        if not vbytes:
            return postings.EmptyVectorReader()
            # raise readers.NoVectorError("This document has no stored vector")
        return self._io.vector_reader(vbytes)

    # Stored fields

    def stored_fields(self, docnum):
        reader = self._cached_reader("_stored", STORED_COLUMN)
        if not reader:
            return {}

        v = reader[docnum]
        if v is None:
            v = {}
        return v


# Terms

class X1FieldWriter(codecs.FieldWriter):
    def __init__(self, session: 'storage.Session', segment: X1Segment,
                 subname: str=None, regionsize: int=255, blocksize: int=128,
                 inlinelimit: int=1):
        self._store = session.store
        self._segment = segment
        self._regionsize = regionsize
        self._blocksize = blocksize
        self._inlinelimit = inlinelimit
        self._subname = subname

        terms_filename = segment.make_filename(X1Codec.TERMS_EXT, subname)
        self._termsfile = self._store.create_file(terms_filename)
        # Write the terms header
        self._termsfile.write(TermsFileHeader(was_little=IS_LITTLE).encode())
        self._termsfile.flush()

        self._termitems = []
        self._refs = []  # type: List[blueline.Ref]

        posts_filename = segment.make_filename(X1Codec.POSTS_EXT, subname)
        self._postsfile = self._store.create_file(posts_filename)
        self._postsfile.write(PostFileHeader(was_little=IS_LITTLE).encode())
        self._postsfile.flush()

        # Assign numbers to fieldnames to shorten keys
        self._fieldnames = []
        self._minterms = {}
        self._maxterms = {}

        # Set by start_field
        self._fieldname = None
        self._fieldnum = None
        self._fieldobj = None
        self._format = None  # type: postform.Format
        self._io = self._segment.codec().postings_io()
        self._infield = False

        # Set by start_term
        self._termbytes = None
        self._terminfo = X1TermInfo()
        self._postbuf = []

        self.closed = False

    def postings_io(self) -> 'postings.PostingsIO':
        return self._io

    def start_field(self, fieldname: str, fieldobj: 'fields.FieldType'):
        assert not self._infield
        self._fieldname = fieldname
        self._fieldobj = fieldobj
        self._format = fieldobj.format
        self._io = self.postings_io()
        self._infield = True
        self._termbytes = None

        self._fieldnum = len(self._fieldnames)
        self._fieldnames.append(fieldname)

    def start_term(self, termbytes: bytes):
        assert self._infield
        assert isinstance(termbytes, bytes)

        if self._termbytes is None:
            self._minterms[self._fieldname] = termbytes

        self._termbytes = termbytes
        self._terminfo = X1TermInfo(offset=self.current_posting_offset())
        self._postbuf = []

    def current_posting_offset(self) -> int:
        return self._postsfile.tell()

    def add_posting(self, post: 'ptuples.PostTuple'):
        self.add_raw_post(self._io.condition_post(post))

    def add_raw_post(self, rawpost: 'ptuples.RawPost'):
        self._postbuf.append(rawpost)
        if len(self._postbuf) >= self._blocksize:
            self._flush_postings()

    def copy_from(self, schema: 'fields.Schema', treader: 'codecs.TermsReader',
                  docmap_get: Callable[[int, int], int]=None,
                  termcount: int=None):
        postsfile = self._postsfile
        tc = 0
        for (fieldname, termbytes), terminfo in treader.items():
            logger.debug("Copying term %s:%r df=%s", fieldname, termbytes,
                         terminfo.doc_frequency())
            assert isinstance(terminfo, X1TermInfo)
            if self._fieldname is not None and fieldname < self._fieldname:
                raise Exception("Out of order fieldnames %s -> %s" %
                                (self._fieldname, fieldname))
            if fieldname != self._fieldname:
                if self._infield:
                    self.finish_field()
                fieldobj = schema[fieldname]
                self.start_field(fieldname, fieldobj)
                logger.info("Copying in field %s", fieldname)

            if self._termbytes is not None and termbytes <= self._termbytes:
                raise Exception("Out of order terms in field %s: %r -> %r" %
                                (fieldname, self._termbytes, termbytes))
            tc += 1
            self.start_term(termbytes)
            self._terminfo.copy_from(terminfo, docmap_get)
            self._terminfo.offset = self.current_posting_offset()
            if terminfo.has_blocks():
                m = treader.matcher(fieldname, termbytes, self._fieldobj)
                for blockbytes in m.raw_blocks():
                    postsfile.write(blockbytes)
                    self._terminfo.blockcount += 1
                m.close()
            self.finish_term()
            logger.debug("Finished copying term")

        if termcount and tc != termcount:
            raise Exception("Field %s should have %s terms but has %s" %
                            (self._fieldname, termcount, tc))
        if self._infield:
            self.finish_field()

    def _flush_postings(self):
        postsfile = self._postsfile
        block = self._postbuf

        # Update term info
        self._terminfo.add_block(block)

        # Write the postings
        postsfile.write(self._io.doclist_to_bytes(self._format, block))
        self._postbuf = []

    def finish_term(self):
        ti = self._terminfo
        fmt = self._format
        postbuf = self._postbuf

        if self._terminfo.inlinebytes is not None:
            # The TermInfo is already inlined! This can happen when we are
            # multi-merging and get a "finished" TermInfo from a worker to add
            # to the final term file.
            pass

        elif self._terminfo.blockcount == 0 and \
                0 < len(postbuf) <= self._inlinelimit:
            # We haven't written any blocks to disk yet, and the number of posts
            # in the buffer is within the inline limit, so include the post(s)
            # inline with the term info
            ti.add_posting_list_stats(postbuf)

            if fmt.only_docids() and len(postbuf) == 1:
                assert ti.doc_frequency() == 1 and ti.min_id() == ti.max_id()
                tibytes = b''
            else:
                tibytes = self._io.doclist_to_bytes(self._format, postbuf)
            ti.inlinebytes = tibytes

        elif len(postbuf):
            # There's postings left in the buffer, flush them to disk
            self._flush_postings()

        elif self._terminfo.blockcount == 0:
            # If we haven't written any blocks to disk, and there's nothing in
            # the buffer, that means there were no posts at all, so just forget
            # the whole thing
            print("NO BLOCKS FOR", self._fieldname, self._termbytes, "!!!!")
            return

        fieldbytes = fieldnum_struct.pack(self._fieldnum)
        keybytes = fieldbytes + cast(bytes, self._termbytes)
        valbytes = ti.to_bytes()
        self._termitems.append((keybytes, valbytes))
        if len(self._termitems) >= self._regionsize:
            self._flush_terms()
        self._postbuf = None

    def _flush_terms(self):
        termitems = self._termitems
        logger.debug("Flushing %s terms %r-%r", len(termitems), termitems[0][0],
                     termitems[-1][0])
        self._refs.append(blueline.write_region(self._termsfile,
                                                self._termitems))
        self._termitems = []

    def finish_field(self):
        assert self._infield
        self._infield = False

        if self._termbytes is not None:
            self._maxterms[self._fieldname] = self._termbytes

    def close(self):
        # If we're still writing a field, finish it off
        if self._infield:
            self.finish_field()

        # Finish the terms and write the region references
        termsfile = self._termsfile
        if self._termitems:
            self._flush_terms()

        # Remember start of refs
        refs_offset = termsfile.tell()
        # Write the refs
        for ref in self._refs:
            termsfile.write(ref.to_bytes())

        # Remember the start of the field names
        names_offset = termsfile.tell()
        # Write the field name lengths
        lens_struct = struct.Struct("<HII")
        for i, fieldname in enumerate(self._fieldnames):
            fbytes = fieldname.encode("utf8")
            minterm = self._minterms.get(fieldname, b'')
            maxterm = self._maxterms.get(fieldname, b'')
            termsfile.write(lens_struct.pack(len(fbytes), len(minterm),
                                             len(maxterm)))
            termsfile.write(fbytes)
            termsfile.write(minterm)
            termsfile.write(maxterm)

        # Write the terms footer
        termsfile.write(TermsFileFooter(
            was_little=IS_LITTLE,
            refs_offset=refs_offset, refs_count=len(self._refs),
            names_offset=names_offset, names_count=len(self._fieldnames),
        ).encode())

        # Close the terms file
        termssize = termsfile.tell()
        termsfile.close()

        # Close the postings file
        postssize = self._postsfile.tell()
        self._postsfile.close()

        # Add the sizes of the terms and postings files to the segment size
        self._segment.add_size(termssize)
        self._segment.add_size(postssize)

        self.closed = True


class X1TermsReader(codecs.TermsReader):
    def __init__(self, session: 'storage.Session', segment: X1Segment,
                 subname: str=None):
        self._store = session.store
        self._segment = segment
        self._subname = subname
        self._io = segment.codec().postings_io()

        self._terms_filename = segment.make_filename(X1Codec.TERMS_EXT, subname)
        data = self._termsdata = self._store.map_file(self._terms_filename)
        # Read the terms header
        terms_header = TermsFileHeader.decode(data)
        assert terms_header.version_number == 0

        self._posts_filename = segment.make_filename(X1Codec.POSTS_EXT, subname)
        self._postsdata = self._store.map_file(self._posts_filename)
        # Read the posts header
        posts_header = PostFileHeader.decode(self._postsdata)
        assert posts_header.version_number == 0

        # Read terms footer
        footer_size = TermsFileFooter.get_size()
        foot = TermsFileFooter.decode(data,
                                      len(data) - footer_size)
        # Read the refs
        refs = []
        pos = foot.refs_offset
        for _ in range(foot.refs_count):
            ref = blueline.Ref.from_bytes(data, pos)
            refs.append(ref)
            pos = ref.end_offset
        # Make a region reader from the refs
        if not refs:
            self._kv = blueline.EmptyRegion()
        elif len(refs) == 1:
            self._kv = blueline.Region.from_ref(data, refs[0])
        else:
            self._kv = blueline.MultiRegion(data, refs)

        # Read field names and min/max terms
        lens_struct = struct.Struct("<HII")
        lens_size = lens_struct.size
        pos = foot.names_offset
        self._fieldnames = []
        self._minmaxterms = []
        for i in range(foot.names_count):
            lens_end = pos + lens_size
            sizes = lens_struct.unpack(data[pos:lens_end])
            fname_len, min_len, max_len = sizes
            pos = lens_end
            fname = bytes(data[pos:pos + fname_len]).decode("utf8")
            self._fieldnames.append(fname)
            pos += fname_len
            minterm = bytes(data[pos:pos + min_len])
            pos += min_len
            maxterm = bytes(data[pos:pos + max_len])
            pos += max_len
            self._minmaxterms.append((minterm, maxterm))

    def _keycoder(self, fieldname: str, tbytes: bytes) -> bytes:
        assert isinstance(tbytes, bytes), "tbytes=%r" % tbytes
        try:
            fnum = self._fieldnames.index(fieldname)
        except ValueError:
            raise reading.TermNotFound("Unknown field %r" % fieldname)
        return fieldnum_struct.pack(fnum) + tbytes

    def _keydecoder(self, keybytes: bytes) -> TermTuple:
        fieldnum = fieldnum_struct.unpack_from(keybytes, 0)[0]
        return self._fieldnames[fieldnum], keybytes[2:]

    def __contains__(self, term: TermTuple) -> bool:
        fieldname, termbytes = term
        if termbytes < self.field_min_term(fieldname) \
                or termbytes > self.field_max_term(fieldname):
            return False
        
        try:
            key = self._keycoder(fieldname, termbytes)
        except reading.TermNotFound:
            return False
        return key in self._kv

    def set_merging_hint(self):
        self._kv.enable_preread()

    def indexed_field_names(self) -> Sequence[str]:
        return list(self._fieldnames)

    def field_min_term(self, fieldname):
        fnum = self._fieldnames.index(fieldname)
        return self._minmaxterms[fnum][0]

    def field_max_term(self, fieldname):
        fnum = self._fieldnames.index(fieldname)
        return self._minmaxterms[fnum][1]

    def cursor(self, fieldname: str, fieldobj: 'fields.FieldType'
               ) -> 'codecs.TermCursor':
        try:
            fnum = self._fieldnames.index(fieldname)
        except ValueError:
            return codecs.EmptyCursor()

        prefix = fieldnum_struct.pack(fnum)
        minterm, maxterm = self._minmaxterms[fnum]
        cur = blueline.SuffixCursor(self._kv.cursor(), prefix, min_key=minterm,
                                    max_key=maxterm)
        return X1TermCursor(cur, fieldobj.to_bytes, fieldobj.from_bytes)

    def terms(self) -> Iterable[TermTuple]:
        keydecoder = self._keydecoder
        return (keydecoder(keybytes) for keybytes in self._kv)

    def term_range(self, fieldname: str, start: bytes, end: Optional[bytes]
                   ) -> Iterable[bytes]:
        # Make sure the start and end are in order
        if end is not None and end < start:
            raise ValueError("start: %r end: %r out of order" % (start, end))

        try:
            # Translate the start and end into keys
            startkey = self._keycoder(fieldname, start)
            # The end can be None (meaning read all available)
            endkey = self._keycoder(fieldname, end) if end is not None else None
        except reading.TermNotFound:
            # The field doesn't exist in the file
            return

        # All keys in this field must start with this prefix
        prefix = self._keycoder(fieldname, b'')

        for key in self._kv.key_range(startkey, endkey):
            if not key.startswith(prefix):
                return
            yield key[2:]

    def items(self) -> Iterable[Tuple[TermTuple, X1TermInfo]]:
        tidecoder = X1TermInfo.from_bytes
        keydecoder = self._keydecoder

        return ((keydecoder(keybytes), tidecoder(valbytes))
                for keybytes, valbytes in self._kv.items())

    def term_info(self, fieldname: str, tbytes: bytes) -> X1TermInfo:
        key = self._keycoder(fieldname, tbytes)
        try:
            return X1TermInfo.from_bytes(self._kv[key])
        except KeyError:
            raise reading.TermNotFound("No term %s:%r" % (fieldname, tbytes))

    def weight(self, fieldname: str, tbytes: bytes) -> float:
        try:
            key = self._keycoder(fieldname, tbytes)
        except reading.TermNotFound:
            return 0

        try:
            data = self._kv[key]
        except KeyError:
            return 0

        return X1TermInfo.decode_weight(data, 0)

    def doc_frequency(self, fieldname: str, tbytes: bytes) -> float:
        try:
            key = self._keycoder(fieldname, tbytes)
        except reading.TermNotFound:
            return 0

        try:
            data = self._kv[key]
        except KeyError:
            return 0

        return X1TermInfo.decode_doc_freq(data, 0)

    def matcher(self, fieldname: str, tbytes: bytes, field: 'fields.FieldType',
                scorer: 'scoring.Scorer'=None) -> 'X1Matcher':
        if not isinstance(field, fields.FieldType):
            raise ValueError("%r is not a field" % field)

        terminfo = self.term_info(fieldname, tbytes)

        fmt = field.format
        is_mini = (fmt.only_docids() and terminfo.doc_frequency() == 1 and
                   terminfo.min_id() == terminfo.max_id())
        if is_mini:
            from whoosh.postings.postings import MinimalDocListReader
            pdr = MinimalDocListReader([terminfo.min_id()])
            m = matchers.PostReaderMatcher(pdr, fieldname, tbytes, terminfo,
                                           self._io, scorer=scorer)
        elif terminfo.inlinebytes:
            pdr = self._io.doclist_reader(terminfo.inlinebytes)
            m = matchers.PostReaderMatcher(pdr, fieldname, tbytes, terminfo,
                                           self._io, scorer=scorer)
        else:
            m = X1Matcher(self._postsdata, fieldname, tbytes, terminfo,
                          self._io, scorer=scorer)

        if m.is_leaf():
            m.set_ranges_are_spans(field.ranges_are_spans())
        return m

    def close(self):
        self._kv.close()
        self._termsdata.close()
        self._postsdata.close()


class X1TermCursor(codecs.TermCursor):
    # This class is a thin wrapper for a blueline cursor

    def __init__(self, cursor: blueline.Cursor,
                 to_bytes: Callable[[Any], bytes],
                 from_bytes: Callable[[bytes], Any]):
        self._cur = cursor
        self._tobytes = to_bytes
        self._frombytes = from_bytes
        self._minterm = cursor.min_key()
        self._maxterm = cursor.max_key()

    def min_term(self) -> bytes:
        return self._minterm

    def max_term(self) -> bytes:
        return self._maxterm

    def first(self):
        self._cur.first()

    def is_valid(self) -> bool:
        return self._cur.is_valid()

    def seek(self, termbytes: bytes):
        if not isinstance(termbytes, bytes):
            termbytes = self._tobytes(termbytes)
        self._cur.seek(termbytes)

    def seek_exact(self, termbytes: bytes) -> bool:
        if not isinstance(termbytes, bytes):
            termbytes = self._tobytes(termbytes)
        return self._cur.seek_exact(termbytes)

    def term_info(self) -> reading.TermInfo:
        try:
            return X1TermInfo.from_bytes(self._cur.value())
        except blueline.InvalidCursor:
            raise codecs.InvalidCursor

    def termbytes(self) -> bytes:
        try:
            return self._cur.key()
        except blueline.InvalidCursor:
            raise codecs.InvalidCursor

    def text(self) -> str:
        return self._frombytes(self._cur.key())

    def next(self):
        try:
            self._cur.next()
        except blueline.InvalidCursor:
            raise codecs.InvalidCursor


class X1Matcher(matchers.LeafMatcher):
    def __init__(self, data: Data, fieldname: str, tbytes: bytes,
                 terminfo: X1TermInfo, postings_io: 'postings.PostingsIO',
                 scorer: 'scoring.Scorer'=None):
        super(X1Matcher, self).__init__(fieldname, tbytes, terminfo,
                                        postings_io, scorer=scorer)

        self._data = data

        # Current offset into postings file
        self._offset = None
        # Total number of posting blocks
        self._blockcount = terminfo.blockcount
        # Current block number
        self._blocknum = 0
        # Go to first offset
        self._go(terminfo.offset)

    def _go(self, offset: int, i: int=0):
        self._offset = offset
        self._posts = self._io.doclist_reader(self._data, self._offset)
        # Index into current block
        self._i = i

    def _next_block(self):
        self._blocknum += 1
        if self._blocknum < self._blockcount:
            self._go(self._offset + self._posts.size_in_bytes())

    def _skip_while(self, test_fn: Callable[[], bool]) -> int:
        skipped = 0
        while self.is_active() and test_fn():
            self._next_block()
            skipped += 1
        return skipped

    def raw_postings(self) -> 'Iterable[matchers.RawPost]':
        while self._blocknum < self._blockcount:
            for rawpost in self._posts.raw_postings():
                yield rawpost
            self._next_block()

    def supports_raw_blocks(self) -> bool:
        return self._posts.supports_raw_blocks()

    def raw_blocks(self) -> Iterable[bytes]:
        while self._blocknum < self._blockcount:
            yield self._posts.raw_bytes()
            self._next_block()

    def is_active(self) -> bool:
        return self._blocknum < self._blockcount

    @matchers.check_active
    def id(self):
        return self._posts.id(self._i)

    @matchers.check_active
    def next(self):
        self._i += 1
        if self._i >= len(self._posts):
            self._next_block()

    @matchers.check_active
    def skip_to(self, docid: int):
        # Skip to block containing the ID
        while self.is_active() and docid > self._posts.max_id():
            self._next_block()

        # Advance within block until we're >= the doc id
        while self.is_active() and self._posts.id(self._i) < docid:
            self.next()

    def save(self) -> Any:
        return self._offset, self._i

    def restore(self, place: Any):
        self._go(*place)

    @matchers.check_active
    def weight(self) -> float:
        if self.has_weights():
            return self._posts.weight(self._i)
        else:
            return 1.0

    @matchers.check_active
    def posting(self) -> 'postings.PostTuple':
        return self._posts.posting_at(self._i, termbytes=self._tbytes)

    @matchers.check_active
    def all_postings(self) -> 'Iterable[ptuples.PostTuple]':
        while self._blocknum < self._blockcount:
            for rp in self._posts.postings():
                yield rp
            self._next_block()

    def raw_posting(self) -> 'ptuples.RawPost':
        return self._posts.raw_posting_at(self._i)

    def all_raw_postings(self) -> 'Iterable[ptuples.RawPost]':
        while self._blocknum < self._blockcount:
            for rp in self._posts.raw_postings():
                yield rp
            self._next_block()

    @matchers.check_active
    def skip_to_quality(self, minquality: float) -> int:
        # If the quality of this block is already higher than the threshold,
        # do nothing
        if self.block_quality() > minquality:
            return 0

        # Skip blocks as long as the block quality is too low
        return self._skip_while(lambda: self.block_quality() <= minquality)

    def all_ids(self):
        while self.is_active():
            for docid in self._posts.all_ids():
                yield docid
            self._next_block()

    # Postings methods

    def length(self) -> int:
        return self._posts.length(self._i)

    def positions(self) -> Sequence[int]:
        return self._posts.positions(self._i)

    def ranges(self) -> Sequence[Tuple[int, int]]:
        return self._posts.ranges(self._i)

    def payloads(self) -> Sequence[bytes]:
        return self._posts.payloads(self._i)

    # Block stats

    def block_min_length(self):
        return self._posts.min_length()

    def block_max_length(self):
        return self._posts.max_length()

    def block_max_weight(self):
        return self._posts.max_weight()


