from __future__ import print_function, division, absolute_import
from fontTools.misc.py23 import *
from fontTools.misc.fixedTools import (
	fixedToFloat as fi2fl, floatToFixed as fl2fi, ensureVersionIsLong as fi2ve,
	versionToFixed as ve2fi)
from fontTools.misc.textTools import pad, safeEval
from fontTools.ttLib import getSearchRange
from .otBase import (CountReference, FormatSwitchingBaseTable,
                     OTTableWriter, ValueRecordFactory)
from .otTables import (AATStateTable, AATState, AATAction,
                       ContextualMorphAction)
from functools import partial
import struct
import logging


log = logging.getLogger(__name__)
istuple = lambda t: isinstance(t, tuple)


def buildConverters(tableSpec, tableNamespace):
	"""Given a table spec from otData.py, build a converter object for each
	field of the table. This is called for each table in otData.py, and
	the results are assigned to the corresponding class in otTables.py."""
	converters = []
	convertersByName = {}
	for tp, name, repeat, aux, descr in tableSpec:
		tableName = name
		if name.startswith("ValueFormat"):
			assert tp == "uint16"
			converterClass = ValueFormat
		elif name.endswith("Count") or name in ("StructLength", "MorphType"):
			converterClass = {
				"uint8": ComputedUInt8,
				"uint16": ComputedUShort,
				"uint32": ComputedULong,
			}[tp]
		elif name == "SubTable":
			converterClass = SubTable
		elif name == "ExtSubTable":
			converterClass = ExtSubTable
		elif name == "SubStruct":
			converterClass = SubStruct
		elif name == "FeatureParams":
			converterClass = FeatureParams
		elif name in ("CIDGlyphMapping", "GlyphCIDMapping"):
			converterClass = StructWithLength
		else:
			if not tp in converterMapping and '(' not in tp:
				tableName = tp
				converterClass = Struct
			else:
				converterClass = eval(tp, tableNamespace, converterMapping)
		if tp in ('MortChain', 'MortSubtable',
		          'MorxChain', 'MorxSubtable'):
			tableClass = tableNamespace.get(tp)
		else:
			tableClass = tableNamespace.get(tableName)
		if tableClass is not None:
			conv = converterClass(name, repeat, aux, tableClass=tableClass)
		else:
			conv = converterClass(name, repeat, aux)
		if name in ["SubTable", "ExtSubTable", "SubStruct"]:
			conv.lookupTypes = tableNamespace['lookupTypes']
			# also create reverse mapping
			for t in conv.lookupTypes.values():
				for cls in t.values():
					convertersByName[cls.__name__] = Table(name, repeat, aux, cls)
		if name == "FeatureParams":
			conv.featureParamTypes = tableNamespace['featureParamTypes']
			conv.defaultFeatureParams = tableNamespace['FeatureParams']
			for cls in conv.featureParamTypes.values():
				convertersByName[cls.__name__] = Table(name, repeat, aux, cls)
		converters.append(conv)
		assert name not in convertersByName, name
		convertersByName[name] = conv
	return converters, convertersByName


class _MissingItem(tuple):
	__slots__ = ()


try:
	from collections import UserList
except ImportError:
	from UserList import UserList


class _LazyList(UserList):

	def __getslice__(self, i, j):
		return self.__getitem__(slice(i, j))

	def __getitem__(self, k):
		if isinstance(k, slice):
			indices = range(*k.indices(len(self)))
			return [self[i] for i in indices]
		item = self.data[k]
		if isinstance(item, _MissingItem):
			self.reader.seek(self.pos + item[0] * self.recordSize)
			item = self.conv.read(self.reader, self.font, {})
			self.data[k] = item
		return item

	def __add__(self, other):
		if isinstance(other, _LazyList):
			other = list(other)
		elif isinstance(other, list):
			pass
		else:
			return NotImplemented
		return list(self) + other

	def __radd__(self, other):
		if not isinstance(other, list):
			return NotImplemented
		return other + list(self)


class BaseConverter(object):

	"""Base class for converter objects. Apart from the constructor, this
	is an abstract class."""

	def __init__(self, name, repeat, aux, tableClass=None):
		self.name = name
		self.repeat = repeat
		self.aux = aux
		self.tableClass = tableClass
		self.isCount = name.endswith("Count") or name in ['DesignAxisRecordSize', 'ValueRecordSize']
		self.isLookupType = name.endswith("LookupType") or name == "MorphType"
		self.isPropagated = name in ["ClassCount", "Class2Count", "FeatureTag", "SettingsCount", "VarRegionCount", "MappingCount", "RegionAxisCount", 'DesignAxisCount', 'DesignAxisRecordSize', 'AxisValueCount', 'ValueRecordSize']

	def readArray(self, reader, font, tableDict, count):
		"""Read an array of values from the reader."""
		lazy = font.lazy and count > 8
		if lazy:
			recordSize = self.getRecordSize(reader)
			if recordSize is NotImplemented:
				lazy = False
		if not lazy:
			l = []
			for i in range(count):
				l.append(self.read(reader, font, tableDict))
			return l
		else:
			l = _LazyList()
			l.reader = reader.copy()
			l.pos = l.reader.pos
			l.font = font
			l.conv = self
			l.recordSize = recordSize
			l.extend(_MissingItem([i]) for i in range(count))
			reader.advance(count * recordSize)
			return l

	def getRecordSize(self, reader):
		if hasattr(self, 'staticSize'): return self.staticSize
		return NotImplemented

	def read(self, reader, font, tableDict):
		"""Read a value from the reader."""
		raise NotImplementedError(self)

	def writeArray(self, writer, font, tableDict, values):
		for i, value in enumerate(values):
			self.write(writer, font, tableDict, value, i)

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		"""Write a value to the writer."""
		raise NotImplementedError(self)

	def xmlRead(self, attrs, content, font):
		"""Read a value from XML."""
		raise NotImplementedError(self)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		"""Write a value to XML."""
		raise NotImplementedError(self)


class SimpleValue(BaseConverter):
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.simpletag(name, attrs + [("value", value)])
		xmlWriter.newline()
	def xmlRead(self, attrs, content, font):
		return attrs["value"]

class IntValue(SimpleValue):
	def xmlRead(self, attrs, content, font):
		return int(attrs["value"], 0)

class Long(IntValue):
	staticSize = 4
	def read(self, reader, font, tableDict):
		return reader.readLong()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeLong(value)

class ULong(IntValue):
	staticSize = 4
	def read(self, reader, font, tableDict):
		return reader.readULong()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeULong(value)

class Flags32(ULong):
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.simpletag(name, attrs + [("value", "0x%08X" % value)])
		xmlWriter.newline()

class Short(IntValue):
	staticSize = 2
	def read(self, reader, font, tableDict):
		return reader.readShort()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeShort(value)

class UShort(IntValue):
	staticSize = 2
	def read(self, reader, font, tableDict):
		return reader.readUShort()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeUShort(value)

class Int8(IntValue):
	staticSize = 1
	def read(self, reader, font, tableDict):
		return reader.readInt8()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeInt8(value)

class UInt8(IntValue):
	staticSize = 1
	def read(self, reader, font, tableDict):
		return reader.readUInt8()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeUInt8(value)

class UInt24(IntValue):
	staticSize = 3
	def read(self, reader, font, tableDict):
		return reader.readUInt24()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeUInt24(value)

class ComputedInt(IntValue):
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		if value is not None:
			xmlWriter.comment("%s=%s" % (name, value))
			xmlWriter.newline()

class ComputedUInt8(ComputedInt, UInt8):
	pass
class ComputedUShort(ComputedInt, UShort):
	pass
class ComputedULong(ComputedInt, ULong):
	pass

class Tag(SimpleValue):
	staticSize = 4
	def read(self, reader, font, tableDict):
		return reader.readTag()
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeTag(value)

class GlyphID(SimpleValue):
	staticSize = 2
	def readArray(self, reader, font, tableDict, count):
		glyphOrder = font.getGlyphOrder()
		gids = reader.readUShortArray(count)
		try:
			l = [glyphOrder[gid] for gid in gids]
		except IndexError:
			# Slower, but will not throw an IndexError on an invalid glyph id.
			l = [font.getGlyphName(gid) for gid in gids]
		return l
	def read(self, reader, font, tableDict):
		return font.getGlyphName(reader.readUShort())
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeUShort(font.getGlyphID(value))


class NameID(UShort):
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.simpletag(name, attrs + [("value", value)])
		nameTable = font.get("name") if font else None
		if nameTable:
			name = nameTable.getDebugName(value)
			xmlWriter.write("  ")
			if name:
				xmlWriter.comment(name)
			else:
				xmlWriter.comment("missing from name table")
				log.warning("name id %d missing from name table" % value)
		xmlWriter.newline()


class FloatValue(SimpleValue):
	def xmlRead(self, attrs, content, font):
		return float(attrs["value"])

class DeciPoints(FloatValue):
	staticSize = 2
	def read(self, reader, font, tableDict):
		return reader.readUShort() / 10

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeUShort(int(round(value * 10)))

class Fixed(FloatValue):
	staticSize = 4
	def read(self, reader, font, tableDict):
		return  fi2fl(reader.readLong(), 16)
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeLong(fl2fi(value, 16))

class F2Dot14(FloatValue):
	staticSize = 2
	def read(self, reader, font, tableDict):
		return  fi2fl(reader.readShort(), 14)
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.writeShort(fl2fi(value, 14))

class Version(BaseConverter):
	staticSize = 4
	def read(self, reader, font, tableDict):
		value = reader.readLong()
		assert (value >> 16) == 1, "Unsupported version 0x%08x" % value
		return value
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		value = fi2ve(value)
		assert (value >> 16) == 1, "Unsupported version 0x%08x" % value
		writer.writeLong(value)
	def xmlRead(self, attrs, content, font):
		value = attrs["value"]
		value = ve2fi(value)
		return value
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		value = fi2ve(value)
		value = "0x%08x" % value
		xmlWriter.simpletag(name, attrs + [("value", value)])
		xmlWriter.newline()

	@staticmethod
	def fromFloat(v):
		return fl2fi(v, 16)


class Char64(SimpleValue):
	"""An ASCII string with up to 64 characters.

	Unused character positions are filled with 0x00 bytes.
	Used in Apple AAT fonts in the `gcid` table.
	"""
	staticSize = 64

	def read(self, reader, font, tableDict):
		data = reader.readData(self.staticSize)
		zeroPos = data.find(b"\0")
		if zeroPos >= 0:
			data = data[:zeroPos]
		s = tounicode(data, encoding="ascii", errors="replace")
		if s != tounicode(data, encoding="ascii", errors="ignore"):
			log.warning('replaced non-ASCII characters in "%s"' %
			            s)
		return s

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		data = tobytes(value, encoding="ascii", errors="replace")
		if data != tobytes(value, encoding="ascii", errors="ignore"):
			log.warning('replacing non-ASCII characters in "%s"' %
			            value)
		if len(data) > self.staticSize:
			log.warning('truncating overlong "%s" to %d bytes' %
			            (value, self.staticSize))
		data = (data + b"\0" * self.staticSize)[:self.staticSize]
		writer.writeData(data)


class Struct(BaseConverter):

	def getRecordSize(self, reader):
		return self.tableClass and self.tableClass.getRecordSize(reader)

	def read(self, reader, font, tableDict):
		table = self.tableClass()
		table.decompile(reader, font)
		return table

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		value.compile(writer, font)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		if value is None:
			if attrs:
				# If there are attributes (probably index), then
				# don't drop this even if it's NULL.  It will mess
				# up the array indices of the containing element.
				xmlWriter.simpletag(name, attrs + [("empty", 1)])
				xmlWriter.newline()
			else:
				pass # NULL table, ignore
		else:
			value.toXML(xmlWriter, font, attrs, name=name)

	def xmlRead(self, attrs, content, font):
		if "empty" in attrs and safeEval(attrs["empty"]):
			return None
		table = self.tableClass()
		Format = attrs.get("Format")
		if Format is not None:
			table.Format = int(Format)

		noPostRead = not hasattr(table, 'postRead')
		if noPostRead:
			# TODO Cache table.hasPropagated.
			cleanPropagation = False
			for conv in table.getConverters():
				if conv.isPropagated:
					cleanPropagation = True
					if not hasattr(font, '_propagator'):
						font._propagator = {}
					propagator = font._propagator
					assert conv.name not in propagator, (conv.name, propagator)
					setattr(table, conv.name, None)
					propagator[conv.name] = CountReference(table.__dict__, conv.name)

		for element in content:
			if isinstance(element, tuple):
				name, attrs, content = element
				table.fromXML(name, attrs, content, font)
			else:
				pass

		table.populateDefaults(propagator=getattr(font, '_propagator', None))

		if noPostRead:
			if cleanPropagation:
				for conv in table.getConverters():
					if conv.isPropagated:
						propagator = font._propagator
						del propagator[conv.name]
						if not propagator:
							del font._propagator

		return table

	def __repr__(self):
		return "Struct of " + repr(self.tableClass)


class StructWithLength(Struct):
	def read(self, reader, font, tableDict):
		pos = reader.pos
		table = self.tableClass()
		table.decompile(reader, font)
		reader.seek(pos + table.StructLength)
		return table

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		for convIndex, conv in enumerate(value.getConverters()):
			if conv.name == "StructLength":
				break
		lengthIndex = len(writer.items) + convIndex
		if isinstance(value, FormatSwitchingBaseTable):
			lengthIndex += 1  # implicit Format field
		deadbeef = {1:0xDE, 2:0xDEAD, 4:0xDEADBEEF}[conv.staticSize]

		before = writer.getDataLength()
		value.StructLength = deadbeef
		value.compile(writer, font)
		length = writer.getDataLength() - before
		lengthWriter = writer.getSubWriter()
		conv.write(lengthWriter, font, tableDict, length)
		assert(writer.items[lengthIndex] ==
		       b"\xde\xad\xbe\xef"[:conv.staticSize])
		writer.items[lengthIndex] = lengthWriter.getAllData()


class Table(Struct):

	longOffset = False
	staticSize = 2

	def readOffset(self, reader):
		return reader.readUShort()

	def writeNullOffset(self, writer):
		if self.longOffset:
			writer.writeULong(0)
		else:
			writer.writeUShort(0)

	def read(self, reader, font, tableDict):
		offset = self.readOffset(reader)
		if offset == 0:
			return None
		table = self.tableClass()
		reader = reader.getSubReader(offset)
		if font.lazy:
			table.reader = reader
			table.font = font
		else:
			table.decompile(reader, font)
		return table

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		if value is None:
			self.writeNullOffset(writer)
		else:
			subWriter = writer.getSubWriter()
			subWriter.longOffset = self.longOffset
			subWriter.name = self.name
			if repeatIndex is not None:
				subWriter.repeatIndex = repeatIndex
			writer.writeSubTable(subWriter)
			value.compile(subWriter, font)

class LTable(Table):

	longOffset = True
	staticSize = 4

	def readOffset(self, reader):
		return reader.readULong()


# TODO Clean / merge the SubTable and SubStruct

class SubStruct(Struct):
	def getConverter(self, tableType, lookupType):
		tableClass = self.lookupTypes[tableType][lookupType]
		return self.__class__(self.name, self.repeat, self.aux, tableClass)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		super(SubStruct, self).xmlWrite(xmlWriter, font, value, None, attrs)

class SubTable(Table):
	def getConverter(self, tableType, lookupType):
		tableClass = self.lookupTypes[tableType][lookupType]
		return self.__class__(self.name, self.repeat, self.aux, tableClass)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		super(SubTable, self).xmlWrite(xmlWriter, font, value, None, attrs)

class ExtSubTable(LTable, SubTable):

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer.Extension = True # actually, mere presence of the field flags it as an Ext Subtable writer.
		Table.write(self, writer, font, tableDict, value, repeatIndex)


class FeatureParams(Table):
	def getConverter(self, featureTag):
		tableClass = self.featureParamTypes.get(featureTag, self.defaultFeatureParams)
		return self.__class__(self.name, self.repeat, self.aux, tableClass)


class ValueFormat(IntValue):
	staticSize = 2
	def __init__(self, name, repeat, aux, tableClass=None):
		BaseConverter.__init__(self, name, repeat, aux, tableClass)
		self.which = "ValueFormat" + ("2" if name[-1] == "2" else "1")
	def read(self, reader, font, tableDict):
		format = reader.readUShort()
		reader[self.which] = ValueRecordFactory(format)
		return format
	def write(self, writer, font, tableDict, format, repeatIndex=None):
		writer.writeUShort(format)
		writer[self.which] = ValueRecordFactory(format)


class ValueRecord(ValueFormat):
	def getRecordSize(self, reader):
		return 2 * len(reader[self.which])
	def read(self, reader, font, tableDict):
		return reader[self.which].readValueRecord(reader, font)
	def write(self, writer, font, tableDict, value, repeatIndex=None):
		writer[self.which].writeValueRecord(writer, font, value)
	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		if value is None:
			pass  # NULL table, ignore
		else:
			value.toXML(xmlWriter, font, self.name, attrs)
	def xmlRead(self, attrs, content, font):
		from .otBase import ValueRecord
		value = ValueRecord()
		value.fromXML(None, attrs, content, font)
		return value


class AATLookup(BaseConverter):
	BIN_SEARCH_HEADER_SIZE = 10

	def __init__(self, name, repeat, aux, tableClass):
		BaseConverter.__init__(self, name, repeat, aux, tableClass)
		if issubclass(self.tableClass, SimpleValue):
			self.converter = self.tableClass(name='Value', repeat=None, aux=None)
		else:
			self.converter = Table(name='Value', repeat=None, aux=None, tableClass=self.tableClass)

	def read(self, reader, font, tableDict):
		format = reader.readUShort()
		if format == 0:
			return self.readFormat0(reader, font)
		elif format == 2:
			return self.readFormat2(reader, font)
		elif format == 4:
			return self.readFormat4(reader, font)
		elif format == 6:
			return self.readFormat6(reader, font)
		elif format == 8:
			return self.readFormat8(reader, font)
		else:
			assert False, "unsupported lookup format: %d" % format

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		values = list(sorted([(font.getGlyphID(glyph), val)
		                      for glyph, val in value.items()]))
		# TODO: Also implement format 4.
		formats = list(sorted(filter(None, [
			self.buildFormat0(writer, font, values),
			self.buildFormat2(writer, font, values),
			self.buildFormat6(writer, font, values),
			self.buildFormat8(writer, font, values),
		])))
		# We use the format ID as secondary sort key to make the output
		# deterministic when multiple formats have same encoded size.
		dataSize, lookupFormat, writeMethod = formats[0]
		pos = writer.getDataLength()
		writeMethod()
		actualSize = writer.getDataLength() - pos
		assert actualSize == dataSize, (
			"AATLookup format %d claimed to write %d bytes, but wrote %d" %
			(lookupFormat, dataSize, actualSize))

	@staticmethod
	def writeBinSearchHeader(writer, numUnits, unitSize):
		writer.writeUShort(unitSize)
		writer.writeUShort(numUnits)
		searchRange, entrySelector, rangeShift = \
			getSearchRange(n=numUnits, itemSize=unitSize)
		writer.writeUShort(searchRange)
		writer.writeUShort(entrySelector)
		writer.writeUShort(rangeShift)

	def buildFormat0(self, writer, font, values):
		numGlyphs = len(font.getGlyphOrder())
		if len(values) != numGlyphs:
			return None
		valueSize = self.converter.staticSize
		return (2 + numGlyphs * valueSize, 0,
			lambda: self.writeFormat0(writer, font, values))

	def writeFormat0(self, writer, font, values):
		writer.writeUShort(0)
		for glyphID_, value in values:
			self.converter.write(
				writer, font, tableDict=None,
				value=value, repeatIndex=None)

	def buildFormat2(self, writer, font, values):
		segStart, segValue = values[0]
		segEnd = segStart
		segments = []
		for glyphID, curValue in values[1:]:
			if glyphID != segEnd + 1 or curValue != segValue:
				segments.append((segStart, segEnd, segValue))
				segStart = segEnd = glyphID
				segValue = curValue
			else:
				segEnd = glyphID
		segments.append((segStart, segEnd, segValue))
		valueSize = self.converter.staticSize
		numUnits, unitSize = len(segments) + 1, valueSize + 4
		return (2 + self.BIN_SEARCH_HEADER_SIZE + numUnits * unitSize, 2,
		        lambda: self.writeFormat2(writer, font, segments))

	def writeFormat2(self, writer, font, segments):
		writer.writeUShort(2)
		valueSize = self.converter.staticSize
		numUnits, unitSize = len(segments), valueSize + 4
		self.writeBinSearchHeader(writer, numUnits, unitSize)
		for firstGlyph, lastGlyph, value in segments:
			writer.writeUShort(lastGlyph)
			writer.writeUShort(firstGlyph)
			self.converter.write(
				writer, font, tableDict=None,
				value=value, repeatIndex=None)
		writer.writeUShort(0xFFFF)
		writer.writeUShort(0xFFFF)
		writer.writeData(b'\x00' * valueSize)

	def buildFormat6(self, writer, font, values):
		valueSize = self.converter.staticSize
		numUnits, unitSize = len(values), valueSize + 2
		return (2 + self.BIN_SEARCH_HEADER_SIZE + (numUnits + 1) * unitSize, 6,
			lambda: self.writeFormat6(writer, font, values))

	def writeFormat6(self, writer, font, values):
		writer.writeUShort(6)
		valueSize = self.converter.staticSize
		numUnits, unitSize = len(values), valueSize + 2
		self.writeBinSearchHeader(writer, numUnits, unitSize)
		for glyphID, value in values:
			writer.writeUShort(glyphID)
			self.converter.write(
				writer, font, tableDict=None,
				value=value, repeatIndex=None)
		writer.writeUShort(0xFFFF)
		writer.writeData(b'\x00' * valueSize)

	def buildFormat8(self, writer, font, values):
		minGlyphID, maxGlyphID = values[0][0], values[-1][0]
		if len(values) != maxGlyphID - minGlyphID + 1:
			return None
		valueSize = self.converter.staticSize
		return (6 + len(values) * valueSize, 8,
                        lambda: self.writeFormat8(writer, font, values))

	def writeFormat8(self, writer, font, values):
		firstGlyphID = values[0][0]
		writer.writeUShort(8)
		writer.writeUShort(firstGlyphID)
		writer.writeUShort(len(values))
		for _, value in values:
			self.converter.write(
				writer, font, tableDict=None,
				value=value, repeatIndex=None)

	def readFormat0(self, reader, font):
		numGlyphs = len(font.getGlyphOrder())
		data = self.converter.readArray(
			reader, font, tableDict=None, count=numGlyphs)
		return {font.getGlyphName(k): value
		        for k, value in enumerate(data)}

	def readFormat2(self, reader, font):
		mapping = {}
		pos = reader.pos - 2  # start of table is at UShort for format
		unitSize, numUnits = reader.readUShort(), reader.readUShort()
		assert unitSize >= 4 + self.converter.staticSize, unitSize
		for i in range(numUnits):
			reader.seek(pos + i * unitSize + 12)
			last = reader.readUShort()
			first = reader.readUShort()
			value = self.converter.read(reader, font, tableDict=None)
			if last != 0xFFFF:
				for k in range(first, last + 1):
					mapping[font.getGlyphName(k)] = value
		return mapping

	def readFormat4(self, reader, font):
		mapping = {}
		pos = reader.pos - 2  # start of table is at UShort for format
		unitSize = reader.readUShort()
		assert unitSize >= 6, unitSize
		for i in range(reader.readUShort()):
			reader.seek(pos + i * unitSize + 12)
			last = reader.readUShort()
			first = reader.readUShort()
			offset = reader.readUShort()
			if last != 0xFFFF:
				dataReader = reader.getSubReader(0)  # relative to current position
				dataReader.seek(pos + offset)  # relative to start of table
				data = self.converter.readArray(
					dataReader, font, tableDict=None,
					count=last - first + 1)
				for k, v in enumerate(data):
					mapping[font.getGlyphName(first + k)] = v
		return mapping

	def readFormat6(self, reader, font):
		mapping = {}
		pos = reader.pos - 2  # start of table is at UShort for format
		unitSize = reader.readUShort()
		assert unitSize >= 2 + self.converter.staticSize, unitSize
		for i in range(reader.readUShort()):
			reader.seek(pos + i * unitSize + 12)
			glyphID = reader.readUShort()
			value = self.converter.read(
				reader, font, tableDict=None)
			if glyphID != 0xFFFF:
				mapping[font.getGlyphName(glyphID)] = value
		return mapping

	def readFormat8(self, reader, font):
		first = reader.readUShort()
		count = reader.readUShort()
		data = self.converter.readArray(
			reader, font, tableDict=None, count=count)
		return {font.getGlyphName(first + k): value
		        for (k, value) in enumerate(data)}

	def xmlRead(self, attrs, content, font):
		value = {}
		for element in content:
			if isinstance(element, tuple):
				name, a, eltContent = element
				if name == "Lookup":
					value[a["glyph"]] = self.converter.xmlRead(a, eltContent, font)
		return value

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.begintag(name, attrs)
		xmlWriter.newline()
		for glyph, value in sorted(value.items()):
			self.converter.xmlWrite(
				xmlWriter, font, value=value,
				name="Lookup", attrs=[("glyph", glyph)])
		xmlWriter.endtag(name)
		xmlWriter.newline()


# The AAT 'ankr' table has an unusual structure: An offset to an AATLookup
# followed by an offset to a glyph data table. Other than usual, the
# offsets in the AATLookup are not relative to the beginning of
# the beginning of the 'ankr' table, but relative to the glyph data table.
# So, to find the anchor data for a glyph, one needs to add the offset
# to the data table to the offset found in the AATLookup, and then use
# the sum of these two offsets to find the actual data.
class AATLookupWithDataOffset(BaseConverter):
	def read(self, reader, font, tableDict):
		lookupOffset = reader.readULong()
		dataOffset = reader.readULong()
		lookupReader = reader.getSubReader(lookupOffset)
		lookup = AATLookup('DataOffsets', None, None, UShort)
		offsets = lookup.read(lookupReader, font, tableDict)
		result = {}
		for glyph, offset in offsets.items():
			dataReader = reader.getSubReader(offset + dataOffset)
			item = self.tableClass()
			item.decompile(dataReader, font)
			result[glyph] = item
		return result

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		# We do not work with OTTableWriter sub-writers because
		# the offsets in our AATLookup are relative to our data
		# table, for which we need to provide an offset value itself.
		# It might have been possible to somehow make a kludge for
		# performing this indirect offset computation directly inside
		# OTTableWriter. But this would have made the internal logic
		# of OTTableWriter even more complex than it already is,
		# so we decided to roll our own offset computation for the
		# contents of the AATLookup and associated data table.
		offsetByGlyph, offsetByData, dataLen = {}, {}, 0
		compiledData = []
		for glyph in sorted(value, key=font.getGlyphID):
			subWriter = OTTableWriter()
			value[glyph].compile(subWriter, font)
			data = subWriter.getAllData()
			offset = offsetByData.get(data, None)
			if offset == None:
				offset = dataLen
				dataLen = dataLen + len(data)
				offsetByData[data] = offset
				compiledData.append(data)
			offsetByGlyph[glyph] = offset
		# For calculating the offsets to our AATLookup and data table,
		# we can use the regular OTTableWriter infrastructure.
		lookupWriter = writer.getSubWriter()
		lookupWriter.longOffset = True
		lookup = AATLookup('DataOffsets', None, None, UShort)
		lookup.write(lookupWriter, font, tableDict, offsetByGlyph, None)

		dataWriter = writer.getSubWriter()
		dataWriter.longOffset = True
		writer.writeSubTable(lookupWriter)
		writer.writeSubTable(dataWriter)
		for d in compiledData:
			dataWriter.writeData(d)

	def xmlRead(self, attrs, content, font):
		lookup = AATLookup('DataOffsets', None, None, self.tableClass)
		return lookup.xmlRead(attrs, content, font)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		lookup = AATLookup('DataOffsets', None, None, self.tableClass)
		lookup.xmlWrite(xmlWriter, font, value, name, attrs)


# https://developer.apple.com/fonts/TrueType-Reference-Manual/RM06/Chap6Tables.html#ExtendedStateHeader
class STXHeader(BaseConverter):
	def __init__(self, name, repeat, aux, tableClass):
		BaseConverter.__init__(self, name, repeat, aux, tableClass)
		assert issubclass(self.tableClass, AATAction)
		self.classLookup = AATLookup("GlyphClasses", None, None, UShort)
		if issubclass(self.tableClass, ContextualMorphAction):
			self.perGlyphLookup = AATLookup("PerGlyphLookup",
			                                None, None, GlyphID)
		else:
			self.perGlyphLookup = None

	def read(self, reader, font, tableDict):
		table = AATStateTable()
		pos = reader.pos
		classTableReader = reader.getSubReader(0)
		stateArrayReader = reader.getSubReader(0)
		entryTableReader = reader.getSubReader(0)
		table.GlyphClassCount = reader.readULong()
		classTableReader.seek(pos + reader.readULong())
		stateArrayReader.seek(pos + reader.readULong())
		entryTableReader.seek(pos + reader.readULong())
		if self.perGlyphLookup is not None:
			perGlyphTableReader = reader.getSubReader(0)
			perGlyphTableReader.seek(pos + reader.readULong())
		table.GlyphClasses = self.classLookup.read(classTableReader,
		                                           font, tableDict)
		numStates = int((entryTableReader.pos - stateArrayReader.pos)
		                 / (table.GlyphClassCount * 2))
		for stateIndex in range(numStates):
			state = AATState()
			table.States.append(state)
			for glyphClass in range(table.GlyphClassCount):
				entryIndex = stateArrayReader.readUShort()
				state.Transitions[glyphClass] = \
					self._readTransition(entryTableReader,
					                     entryIndex, font)
		if self.perGlyphLookup is not None:
			table.PerGlyphLookups = self._readPerGlyphLookups(
				table, perGlyphTableReader, font)
		return table

	def _readTransition(self, reader, entryIndex, font):
		transition = self.tableClass()
		entryReader = reader.getSubReader(
			reader.pos + entryIndex * transition.staticSize)
		transition.decompile(entryReader, font)
		return transition

	def _countPerGlyphLookups(self, table):
		# Somewhat annoyingly, the morx table does not encode
		# the size of the per-glyph table. So we need to find
		# the maximum value that MorphActions use as index
		# into this table.
		numLookups = 0
		for state in table.States:
			for t in state.Transitions.values():
				if isinstance(t, ContextualMorphAction):
					if t.MarkIndex != 0xFFFF:
						numLookups = max(
							numLookups,
							t.MarkIndex + 1)
					if t.CurrentIndex != 0xFFFF:
						numLookups = max(
							numLookups,
							t.CurrentIndex + 1)
		return numLookups

	def _readPerGlyphLookups(self, table, reader, font):
		pos = reader.pos
		lookups = []
		for _ in range(self._countPerGlyphLookups(table)):
			lookupReader = reader.getSubReader(0)
			lookupReader.seek(pos + reader.readULong())
			lookups.append(
				self.perGlyphLookup.read(lookupReader, font, {}))
		return lookups

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		glyphClassWriter = OTTableWriter()
		self.classLookup.write(glyphClassWriter, font, tableDict,
		                       value.GlyphClasses, repeatIndex=None)
		glyphClassData = pad(glyphClassWriter.getAllData(), 4)
		glyphClassCount = max(value.GlyphClasses.values()) + 1
		glyphClassTableOffset = 16  # size of STXHeader
		if self.perGlyphLookup is not None:
			glyphClassTableOffset += 4
		stateArrayWriter = OTTableWriter()
		entries, entryIDs = [], {}
		for state in value.States:
			for glyphClass in range(glyphClassCount):
				transition = state.Transitions[glyphClass]
				entryWriter = OTTableWriter()
				transition.compile(entryWriter, font)
				entryData = entryWriter.getAllData()
				assert len(entryData)  == transition.staticSize, ( \
					"%s has staticSize %d, "
					"but actually wrote %d bytes" % (
						repr(transition),
						transition.staticSize,
						len(entryData)))
				entryIndex = entryIDs.get(entryData)
				if entryIndex is None:
					entryIndex = len(entries)
					entryIDs[entryData] = entryIndex
					entries.append(entryData)
				stateArrayWriter.writeUShort(entryIndex)
		stateArrayOffset = glyphClassTableOffset + len(glyphClassData)
		stateArrayData = pad(stateArrayWriter.getAllData(), 4)
		entryTableOffset = stateArrayOffset + len(stateArrayData)
		entryTableData = pad(bytesjoin(entries), 4)
		perGlyphOffset = entryTableOffset + len(entryTableData)
		perGlyphData = \
			pad(self._compilePerGlyphLookups(value, font), 4)
		writer.writeULong(glyphClassCount)
		writer.writeULong(glyphClassTableOffset)
		writer.writeULong(stateArrayOffset)
		writer.writeULong(entryTableOffset)
		if self.perGlyphLookup is not None:
			writer.writeULong(perGlyphOffset)
		writer.writeData(glyphClassData)
		writer.writeData(stateArrayData)
		writer.writeData(entryTableData)
		writer.writeData(perGlyphData)

	def _compilePerGlyphLookups(self, table, font):
		if self.perGlyphLookup is None:
			return b""
		numLookups = self._countPerGlyphLookups(table)
		assert len(table.PerGlyphLookups) == numLookups, (
			"len(AATStateTable.PerGlyphLookups) is %d, "
			"but the actions inside the table refer to %d" %
				(len(table.PerGlyphLookups), numLookups))
		writer = OTTableWriter()
		for lookup in table.PerGlyphLookups:
			lookupWriter = writer.getSubWriter()
			lookupWriter.longOffset = True
			self.perGlyphLookup.write(lookupWriter, font,
			                          {}, lookup, None)
			writer.writeSubTable(lookupWriter)
		return writer.getAllData()

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.begintag(name, attrs)
		xmlWriter.newline()
		xmlWriter.comment("GlyphClassCount=%s" %value.GlyphClassCount)
		xmlWriter.newline()
		for g, klass in sorted(value.GlyphClasses.items()):
			xmlWriter.simpletag("GlyphClass", glyph=g, value=klass)
			xmlWriter.newline()
		for stateIndex, state in enumerate(value.States):
			xmlWriter.begintag("State", index=stateIndex)
			xmlWriter.newline()
			for glyphClass, trans in sorted(state.Transitions.items()):
				trans.toXML(xmlWriter, font=font,
				            attrs={"onGlyphClass": glyphClass},
				            name="Transition")
			xmlWriter.endtag("State")
			xmlWriter.newline()
		for i, lookup in enumerate(value.PerGlyphLookups):
			xmlWriter.begintag("PerGlyphLookup", index=i)
			xmlWriter.newline()
			for glyph, val in sorted(lookup.items()):
				xmlWriter.simpletag("Lookup", glyph=glyph,
				                    value=val)
				xmlWriter.newline()
			xmlWriter.endtag("PerGlyphLookup")
			xmlWriter.newline()
		xmlWriter.endtag(name)
		xmlWriter.newline()

	def xmlRead(self, attrs, content, font):
		table = AATStateTable()
		for eltName, eltAttrs, eltContent in filter(istuple, content):
			if eltName == "GlyphClass":
				glyph = eltAttrs["glyph"]
				value = eltAttrs["value"]
				table.GlyphClasses[glyph] = safeEval(value)
			elif eltName == "State":
				state = self._xmlReadState(eltAttrs, eltContent, font)
				table.States.append(state)
			elif eltName == "PerGlyphLookup":
				lookup = self.perGlyphLookup.xmlRead(
					eltAttrs, eltContent, font)
				table.PerGlyphLookups.append(lookup)
		table.GlyphClassCount = max(table.GlyphClasses.values()) + 1
		return table

	def _xmlReadState(self, attrs, content, font):
		state = AATState()
		for eltName, eltAttrs, eltContent in filter(istuple, content):
			if eltName == "Transition":
				glyphClass = safeEval(eltAttrs["onGlyphClass"])
				transition = self.tableClass()
				transition.fromXML(eltName, eltAttrs,
				                   eltContent, font)
				state.Transitions[glyphClass] = transition
		return state


class CIDGlyphMap(BaseConverter):
	def read(self, reader, font, tableDict):
		numCIDs = reader.readUShort()
		result = {}
		for cid, glyphID in enumerate(reader.readUShortArray(numCIDs)):
			if glyphID != 0xFFFF:
				result[cid] = font.getGlyphName(glyphID)
		return result

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		items = {cid: font.getGlyphID(glyph)
		         for cid, glyph in value.items()}
		count = max(items) + 1 if items else 0
		writer.writeUShort(count)
		for cid in range(count):
			writer.writeUShort(items.get(cid, 0xFFFF))

	def xmlRead(self, attrs, content, font):
		result = {}
		for eName, eAttrs, _eContent in filter(istuple, content):
			if eName == "CID":
				result[safeEval(eAttrs["cid"])] = \
					eAttrs["glyph"].strip()
		return result

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.begintag(name, attrs)
		xmlWriter.newline()
		for cid, glyph in sorted(value.items()):
			if glyph is not None and glyph != 0xFFFF:
				xmlWriter.simpletag(
					"CID", cid=cid, glyph=glyph)
				xmlWriter.newline()
		xmlWriter.endtag(name)
		xmlWriter.newline()


class GlyphCIDMap(BaseConverter):
	def read(self, reader, font, tableDict):
		glyphOrder = font.getGlyphOrder()
		count = reader.readUShort()
		cids = reader.readUShortArray(count)
		if count > len(glyphOrder):
			log.warning("GlyphCIDMap has %d elements, "
			            "but the font has only %d glyphs; "
			            "ignoring the rest" %
			             (count, len(glyphOrder)))
		result = {}
		for glyphID in range(min(len(cids), len(glyphOrder))):
			cid = cids[glyphID]
			if cid != 0xFFFF:
				result[glyphOrder[glyphID]] = cid
		return result

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		items = {font.getGlyphID(g): cid
		         for g, cid in value.items()
		         if cid is not None and cid != 0xFFFF}
		count = max(items) + 1 if items else 0
		writer.writeUShort(count)
		for glyphID in range(count):
			writer.writeUShort(items.get(glyphID, 0xFFFF))

	def xmlRead(self, attrs, content, font):
		result = {}
		for eName, eAttrs, _eContent in filter(istuple, content):
			if eName == "CID":
				result[eAttrs["glyph"]] = \
					safeEval(eAttrs["value"])
		return result

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.begintag(name, attrs)
		xmlWriter.newline()
		for glyph, cid in sorted(value.items()):
			if cid is not None and cid != 0xFFFF:
				xmlWriter.simpletag(
					"CID", glyph=glyph, value=cid)
				xmlWriter.newline()
		xmlWriter.endtag(name)
		xmlWriter.newline()


class DeltaValue(BaseConverter):

	def read(self, reader, font, tableDict):
		StartSize = tableDict["StartSize"]
		EndSize = tableDict["EndSize"]
		DeltaFormat = tableDict["DeltaFormat"]
		assert DeltaFormat in (1, 2, 3), "illegal DeltaFormat"
		nItems = EndSize - StartSize + 1
		nBits = 1 << DeltaFormat
		minusOffset = 1 << nBits
		mask = (1 << nBits) - 1
		signMask = 1 << (nBits - 1)

		DeltaValue = []
		tmp, shift = 0, 0
		for i in range(nItems):
			if shift == 0:
				tmp, shift = reader.readUShort(), 16
			shift = shift - nBits
			value = (tmp >> shift) & mask
			if value & signMask:
				value = value - minusOffset
			DeltaValue.append(value)
		return DeltaValue

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		StartSize = tableDict["StartSize"]
		EndSize = tableDict["EndSize"]
		DeltaFormat = tableDict["DeltaFormat"]
		DeltaValue = value
		assert DeltaFormat in (1, 2, 3), "illegal DeltaFormat"
		nItems = EndSize - StartSize + 1
		nBits = 1 << DeltaFormat
		assert len(DeltaValue) == nItems
		mask = (1 << nBits) - 1

		tmp, shift = 0, 16
		for value in DeltaValue:
			shift = shift - nBits
			tmp = tmp | ((value & mask) << shift)
			if shift == 0:
				writer.writeUShort(tmp)
				tmp, shift = 0, 16
		if shift != 16:
			writer.writeUShort(tmp)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.simpletag(name, attrs + [("value", value)])
		xmlWriter.newline()

	def xmlRead(self, attrs, content, font):
		return safeEval(attrs["value"])


class VarIdxMapValue(BaseConverter):

	def read(self, reader, font, tableDict):
		fmt = tableDict['EntryFormat']
		nItems = tableDict['MappingCount']

		innerBits = 1 + (fmt & 0x000F)
		innerMask = (1<<innerBits) - 1
		outerMask = 0xFFFFFFFF - innerMask
		outerShift = 16 - innerBits

		entrySize = 1 + ((fmt & 0x0030) >> 4)
		read = {
			1: reader.readUInt8,
			2: reader.readUShort,
			3: reader.readUInt24,
			4: reader.readULong,
		}[entrySize]

		mapping = []
		for i in range(nItems):
			raw = read()
			idx = ((raw & outerMask) << outerShift) | (raw & innerMask)
			mapping.append(idx)

		return mapping

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		fmt = tableDict['EntryFormat']
		mapping = value
		writer['MappingCount'].setValue(len(mapping))

		innerBits = 1 + (fmt & 0x000F)
		innerMask = (1<<innerBits) - 1
		outerShift = 16 - innerBits

		entrySize = 1 + ((fmt & 0x0030) >> 4)
		write = {
			1: writer.writeUInt8,
			2: writer.writeUShort,
			3: writer.writeUInt24,
			4: writer.writeULong,
		}[entrySize]

		for idx in mapping:
			raw = ((idx & 0xFFFF0000) >> outerShift) | (idx & innerMask)
			write(raw)


class VarDataValue(BaseConverter):

	def read(self, reader, font, tableDict):
		values = []

		regionCount = tableDict["VarRegionCount"]
		shortCount = tableDict["NumShorts"]

		for i in range(min(regionCount, shortCount)):
			values.append(reader.readShort())
		for i in range(min(regionCount, shortCount), regionCount):
			values.append(reader.readInt8())
		for i in range(regionCount, shortCount):
			reader.readInt8()

		return values

	def write(self, writer, font, tableDict, value, repeatIndex=None):
		regionCount = tableDict["VarRegionCount"]
		shortCount = tableDict["NumShorts"]

		for i in range(min(regionCount, shortCount)):
			writer.writeShort(value[i])
		for i in range(min(regionCount, shortCount), regionCount):
			writer.writeInt8(value[i])
		for i in range(regionCount, shortCount):
			writer.writeInt8(0)

	def xmlWrite(self, xmlWriter, font, value, name, attrs):
		xmlWriter.simpletag(name, attrs + [("value", value)])
		xmlWriter.newline()

	def xmlRead(self, attrs, content, font):
		return safeEval(attrs["value"])


converterMapping = {
	# type		class
	"int8":		Int8,
	"int16":	Short,
	"uint8":	UInt8,
	"uint8":	UInt8,
	"uint16":	UShort,
	"uint24":	UInt24,
	"uint32":	ULong,
	"char64":	Char64,
	"Flags32":	Flags32,
	"Version":	Version,
	"Tag":		Tag,
	"GlyphID":	GlyphID,
	"NameID":	NameID,
	"DeciPoints":	DeciPoints,
	"Fixed":	Fixed,
	"F2Dot14":	F2Dot14,
	"struct":	Struct,
	"Offset":	Table,
	"LOffset":	LTable,
	"ValueRecord":	ValueRecord,
	"DeltaValue":	DeltaValue,
	"VarIdxMapValue":	VarIdxMapValue,
	"VarDataValue":	VarDataValue,

	# AAT
	"CIDGlyphMap":	CIDGlyphMap,
	"GlyphCIDMap":	GlyphCIDMap,
	"MortChain":	StructWithLength,
	"MortSubtable": StructWithLength,
	"MorxChain":	StructWithLength,
	"MorxSubtable": StructWithLength,

	# "Template" types
	"AATLookup":	lambda C: partial(AATLookup, tableClass=C),
	"AATLookupWithDataOffset":	lambda C: partial(AATLookupWithDataOffset, tableClass=C),
	"STXHeader":	lambda C: partial(STXHeader, tableClass=C),
	"OffsetTo":	lambda C: partial(Table, tableClass=C),
	"LOffsetTo":	lambda C: partial(LTable, tableClass=C),
}
