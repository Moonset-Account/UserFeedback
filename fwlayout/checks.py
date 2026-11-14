"""Layout checks over a parsed header.

All ranges are half-open [start, end): a partition whose end equals the
next partition's start is adjacent, NOT overlapping.  The same rule
applies to board reserved regions.
"""


def _usable_records(header):
    """Records complete enough to carry a start/length pair."""
    out = []
    for rec in header.records:
        if rec.truncated:
            continue
        start = rec.fields.get("start")
        length = rec.fields.get("length")
        if start is None or length is None:
            continue
        out.append(rec)
    return out


def check_image(header, image_size, board):
    """Run all layout checks; returns a list of finding dicts."""
    findings = []

    for rec in header.records:
        if rec.truncated:
            findings.append({
                "type": "truncated_record",
                "record_index": rec.index,
                "record_offset": rec.offset,
                "detail": "only %d of %d record bytes present in image"
                          % (rec.available, header.record_size),
            })
    if header.missing_records:
        findings.append({
            "type": "missing_records",
            "count": header.missing_records,
            "detail": "declared record count %d exceeds the %d record(s) "
                      "present in the image"
                      % (header.record_count, len(header.records)),
        })

    parts = _usable_records(header)

    # Ranges extending past the end of the image.
    for rec in parts:
        start = rec.fields["start"]
        end = start + rec.fields["length"]
        if end > image_size:
            findings.append({
                "type": "out_of_image",
                "record_index": rec.index,
                "record_offset": rec.offset,
                "range": [start, end],
                "image_size": image_size,
                "detail": "partition range [%d, %d) extends past the "
                          "%d-byte image" % (start, end, image_size),
            })

    # Pairwise overlaps between partitions (half-open ranges, so a
    # partition starting exactly where another ends is adjacent).
    ordered = sorted(parts, key=lambda r: (r.fields["start"], r.offset))
    for i, a in enumerate(ordered):
        a_start = a.fields["start"]
        a_end = a_start + a.fields["length"]
        for b in ordered[i + 1:]:
            b_start = b.fields["start"]
            if b_start >= a_end:
                break  # sorted by start: no later record can overlap a
            b_end = b_start + b.fields["length"]
            findings.append({
                "type": "overlap",
                "record_offsets": [a.offset, b.offset],
                "record_indexes": [a.index, b.index],
                "overlap_range": [b_start, min(a_end, b_end)],
                "partitions": [
                    {"record_offset": a.offset, "range": [a_start, a_end]},
                    {"record_offset": b.offset, "range": [b_start, b_end]},
                ],
                "detail": "records at offsets %d and %d overlap over "
                          "[%d, %d)" % (a.offset, b.offset, b_start,
                                        min(a_end, b_end)),
            })

    # Partitions covering board reserved regions.
    for rec in parts:
        start = rec.fields["start"]
        length = rec.fields["length"]
        if length == 0:
            continue
        end = start + length
        for region in board.reserved_regions:
            if start < region.end and region.start < end:
                findings.append({
                    "type": "reserved_region",
                    "record_index": rec.index,
                    "record_offset": rec.offset,
                    "region": region.name,
                    "region_range": [region.start, region.end],
                    "partition_range": [start, end],
                    "detail": "partition [%d, %d) covers reserved region "
                              "'%s' [%d, %d)"
                              % (start, end, region.name, region.start,
                                 region.end),
                })

    return findings
