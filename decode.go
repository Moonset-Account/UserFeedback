package causalcodec

import (
	"encoding/binary"
	"hash/crc32"
	"io"
)

// Decode parses a packet and resolves anchors using c.Anchors. When an anchor
// is unavailable, it returns a non-nil Report containing all independently
// decodable paths plus a *PartialDecodeError.
func (c *Codec) Decode(data []byte) (*Report, error) {
	if len(data) < 13 {
		return nil, malformed("short header: %d bytes", len(data))
	}
	if string(data[:4]) != wireMagic {
		return nil, malformed("bad magic %q", string(data[:4]))
	}
	if data[4] != wireVersion {
		return nil, malformed("unsupported version %d", data[4])
	}
	bodyLen := binary.BigEndian.Uint32(data[5:9])
	storedCRC := binary.BigEndian.Uint32(data[9:13])
	if uint64(bodyLen) != uint64(len(data)-13) {
		return nil, malformed("body length %d does not match packet size %d", bodyLen, len(data)-13)
	}
	body := data[13:]
	if crc32.ChecksumIEEE(body) != storedCRC {
		return nil, malformed("crc32 mismatch")
	}

	var anchors AnchorResolver
	if c != nil {
		anchors = c.Anchors
	}
	r := newWireReader(body)
	rep, branchErrs := decodeBody(r, anchors)
	if r.err != nil {
		return rep, r.err
	}
	if len(branchErrs) > 0 {
		return rep, &PartialDecodeError{Errors: branchErrs}
	}
	return rep, nil
}

func decodeBody(r *wireReader, anchors AnchorResolver) (*Report, []error) {
	rep := &Report{}
	rep.Delivery.ID = r.str()
	rep.Delivery.EventID = r.str()
	rep.Delivery.Source = r.source()

	causeCount := r.u32()
	if uint64(causeCount) > uint64(r.Len()) {
		r.err = malformed("cause count %d exceeds packet bytes", causeCount)
		return rep, nil
	}
	for i := uint32(0); i < causeCount && r.err == nil; i++ {
		rep.Causes = append(rep.Causes, r.cause())
	}
	if r.err != nil {
		return rep, nil
	}

	pathCount := r.u32()
	if uint64(pathCount) > uint64(r.Len()) {
		r.err = malformed("path count %d exceeds packet bytes", pathCount)
		return rep, nil
	}

	var branchErrs []error
	for i := uint32(0); i < pathCount; i++ {
		blockLen := r.u32()
		if r.err != nil {
			return rep, branchErrs
		}
		if uint64(blockLen) > uint64(r.Len()) {
			r.err = malformed("path %d block length %d exceeds remaining %d", i, blockLen, r.Len())
			return rep, branchErrs
		}
		block := make([]byte, blockLen)
		if _, err := io.ReadFull(r.Reader, block); err != nil {
			r.err = malformed("read path block: %v", err)
			return rep, branchErrs
		}

		br := newWireReader(block)
		p, errs := decodePathBlock(br, rep, anchors)
		if p != nil {
			rep.Paths = append(rep.Paths, *p)
		}
		if br.err != nil {
			pathID := ""
			if p != nil {
				pathID = p.ID
			}
			errs = append(errs, &BranchError{PathID: pathID, Err: br.err})
		}
		branchErrs = append(branchErrs, errs...)
	}
	return rep, branchErrs
}

func decodePathBlock(r *wireReader, rep *Report, anchors AnchorResolver) (*Path, []error) {
	p := &Path{ID: r.str(), Label: r.str()}
	segmentCount := r.u16()
	var errs []error

	for si := 0; si < segmentCount && r.err == nil; si++ {
		stage := r.str()
		flags := r.readByteValue()
		idCount := r.u16()

		var ids []string
		for j := 0; j < idCount; j++ {
			idx := r.u16()
			if r.err != nil {
				break
			}
			if idx >= len(rep.Causes) {
				errs = append(errs, &BranchError{
					PathID: p.ID, Stage: stage,
					Err: malformed("cause index %d out of range (table=%d)", idx, len(rep.Causes)),
				})
				continue
			}
			ids = append(ids, rep.Causes[idx].ID)
		}

		omittedCount := r.u16()
		var omitted []OmittedCause
		for j := 0; j < omittedCount && r.err == nil; j++ {
			omitted = append(omitted, OmittedCause{
				ID:       r.str(),
				Priority: r.i32(),
				Reason:   omissionReasonFromCode(r.readByteValue()),
			})
		}

		var anchorRef AnchorRef
		hasAnchor := flags&flagSegmentAnchor != 0
		if hasAnchor {
			anchorRef = r.ref()
		}

		var gapKind GapKind
		var gapReason string
		var threshold Priority
		hasGap := flags&flagSegmentGap != 0
		isExtended := flags&flagSegmentExtended != 0
		var extensionReason string
		if hasGap {
			gapKind = gapKindFromCode(r.readByteValue())
			gapReason = defaultGapReason(gapKind)
			threshold = r.i32()
			if isExtended {
				extensionReason = r.str()
			}
		}
		if r.err != nil {
			return p, errs
		}

		gapID := gapID(p.ID, si, stage)

		if hasAnchor {
			anchor, err := resolveAnchor(anchors, anchorRef)
			if err != nil {
				errs = append(errs, &BranchError{PathID: p.ID, Stage: stage, Err: err})
				seg := Segment{
					Stage:   stage,
					Anchor:  &anchorRef,
					Omitted: omitted,
					Gap: &Gap{
						ID:              gapID,
						Kind:            GapMissingAnchor,
						Reason:          "exact anchor unavailable: " + anchorRef.String(),
						Omitted:         append([]OmittedCause(nil), omitted...),
						Threshold:       threshold,
						Anchor:          &anchorRef,
						Extended:        isExtended,
						ExtensionReason: extensionReason,
					},
				}
				p.Segments = append(p.Segments, seg)
				continue
			}

			mergeAnchorCauses(rep, anchor, p.ID, stage, &errs)
			ref := anchor.Ref
			if len(anchor.Context.Segments) > 0 {
				for _, as := range anchor.Context.Segments {
					seg := as
					seg.CauseIDs = append([]string(nil), as.CauseIDs...)
					seg.Omitted = append([]OmittedCause(nil), as.Omitted...)
					rf := ref
					seg.ResolvedFrom = &rf
					p.Segments = append(p.Segments, seg)
				}
			} else {
				seg := Segment{Stage: stage, ResolvedFrom: &ref}
				for _, c := range anchor.Context.Causes {
					seg.CauseIDs = append(seg.CauseIDs, c.ID)
				}
				p.Segments = append(p.Segments, seg)
			}
			continue
		}

		seg := Segment{Stage: stage, CauseIDs: ids, Omitted: omitted}
		if hasGap {
			seg.Gap = &Gap{
				ID:              gapID,
				Kind:            gapKind,
				Reason:          gapReason,
				Omitted:         append([]OmittedCause(nil), omitted...),
				Threshold:       threshold,
				Extended:        isExtended,
				ExtensionReason: extensionReason,
			}
		}
		p.Segments = append(p.Segments, seg)
	}
	return p, errs
}

func resolveAnchor(r AnchorResolver, ref AnchorRef) (*Anchor, error) {
	if r == nil {
		return nil, &MissingAnchorError{Ref: ref}
	}
	return r.ResolveAnchor(ref)
}

func mergeAnchorCauses(rep *Report, anchor *Anchor, pathID, stage string, errs *[]error) {
	for _, c := range anchor.Context.Causes {
		existing, ok := rep.Cause(c.ID)
		if !ok {
			rep.Causes = append(rep.Causes, c)
			continue
		}
		if !causesEqual(existing, c) {
			*errs = append(*errs, &BranchError{
				PathID: pathID,
				Stage:  stage,
				Err:    malformed("anchor %s defines conflicting cause %q", anchor.Ref, c.ID),
			})
		}
	}
}
