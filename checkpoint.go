package fecrx

import (
	"bytes"
	"encoding/gob"
)

// The DTO types use sorted slices so checkpoints are deterministic
// and inspectable.

type rowDTO struct {
	Coeffs []uint64
	RHS    []byte
	Label  []uint64
}

type wfactDTO struct {
	Support []uint64
	Const   []byte
	Sources []uint64
}

type checkpointDTO struct {
	Base       uint64
	Size       uint64
	DeliverPtr uint64

	Rows []rowDTO
	W    []wfactDTO
	Z    [][]uint64

	Confirmed map[uint64][]byte
	Meta      map[uint64]int
	Sources   map[uint64]uint64

	Frozen bool
}

// Checkpoint returns a self-contained, restorable snapshot of the
// receiver (basis, discarded facts, source dependencies, confirmed
// prefix, metadata and revisions). Hooks are not part of the snapshot;
// pass fresh hooks to Restore.
func (r *Receiver) Checkpoint() ([]byte, error) {
	dto := checkpointDTO{
		Base:       r.base,
		Size:       r.size,
		DeliverPtr: r.deliverPtr,
		Confirmed:  map[uint64][]byte{},
		Meta:       map[uint64]int{},
		Sources:    map[uint64]uint64{},
		Frozen:     r.frozen,
	}
	for _, rw := range r.rows {
		dto.Rows = append(dto.Rows, rowDTO{
			Coeffs: rw.coeffs.sorted(),
			RHS:    append([]byte(nil), rw.rhs...),
			Label:  rw.label.sorted(),
		})
	}
	for _, wf := range r.w {
		dto.W = append(dto.W, wfactDTO{
			Support: wf.support.sorted(),
			Const:   append([]byte(nil), wf.konst...),
			Sources: wf.sources.sorted(),
		})
	}
	for _, zr := range r.z {
		dto.Z = append(dto.Z, zr.sorted())
	}
	for k, v := range r.confirmed {
		dto.Confirmed[k] = append([]byte(nil), v...)
	}
	for k, v := range r.meta {
		dto.Meta[k] = v
	}
	for k, v := range r.sources {
		dto.Sources[k] = v
	}

	var buf bytes.Buffer
	if err := gob.NewEncoder(&buf).Encode(&dto); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// Restore decodes a checkpoint produced by Checkpoint and attaches
// h. The restored receiver does not replay delivery/gap callbacks:
// the checkpoint already contains the committed prefix.
func Restore(data []byte, h Hooks) (*Receiver, error) {
	var dto checkpointDTO
	if err := gob.NewDecoder(bytes.NewReader(data)).Decode(&dto); err != nil {
		return nil, err
	}

	r := &Receiver{
		base:       dto.Base,
		size:       dto.Size,
		deliverPtr: dto.DeliverPtr,
		confirmed:  dto.Confirmed,
		meta:       dto.Meta,
		sources:    dto.Sources,
		frozen:     dto.Frozen,
		hooks:      h,
	}
	if r.confirmed == nil {
		r.confirmed = map[uint64][]byte{}
	}
	if r.meta == nil {
		r.meta = map[uint64]int{}
	}
	if r.sources == nil {
		r.sources = map[uint64]uint64{}
	}

	for _, rd := range dto.Rows {
		r.rows = append(r.rows, &row{
			coeffs: newSet(rd.Coeffs...),
			rhs:    append([]byte(nil), rd.RHS...),
			label:  newSet(rd.Label...),
		})
	}
	sortRows(r.rows)

	for _, wd := range dto.W {
		r.w = append(r.w, &wfact{
			support: newSet(wd.Support...),
			konst:   append([]byte(nil), wd.Const...),
			sources: newSet(wd.Sources...),
		})
	}
	sortW(r.w)

	for _, zd := range dto.Z {
		r.z = append(r.z, newSet(zd...))
	}
	sortZ(r.z)

	return r, nil
}
