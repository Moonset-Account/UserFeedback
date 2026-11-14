"""Read-only firmware partition layout checker.

Parses only the image header and partition table of a firmware image
(never the partition contents) and reports layout problems: truncated
records, ranges extending past the image, overlapping partitions and
partitions covering board-reserved regions.
"""

__version__ = "1"
