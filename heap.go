// Package heap implements page selection and slot placement for inserting
// records into a heap table.
//
// A free-space directory keeps approximate per-page free space, quantized
// into capacity tiers. An insert first asks the directory for a candidate
// page, then — under the page lock, against the page's real state — decides
// to place the record directly, compact the page once and place it, or give
// up on the page (correcting the directory hint) and pick another page.
// Compaction never changes the slot numbers of live records, so previously
// returned locations stay valid.
package heap

import "sync"

const (
	// pageHeaderSize is the fixed per-page overhead in bytes.
	pageHeaderSize = 8
	// linePointerSize is the per-slot overhead in bytes.
	linePointerSize = 4
)

// lpState is the state of a line pointer (slot).
type lpState uint8

const (
	lpUnused lpState = iota // never used, or reclaimed by compaction; reusable
	lpLive                  // holds a live record
	lpDead                  // deleted; bytes still occupy the page (a fragment)
)

// linePointer describes one slot: where its tuple bytes live and how long
// they are. Slot numbers are stable identifiers: compaction may rewrite
// offsets but never reorders or renumbers slots.
type linePointer struct {
	state  lpState
	offset int // byte offset of the tuple within the page
	length int // tuple length in bytes
}

// Page is a fixed-size slotted page. Line pointers grow up from the header;
// tuple bytes grow down from the end of the page. The bytes between are the
// contiguous free gap; dead tuples inside the data area are fragments that
// only compaction reclaims.
type Page struct {
	mu       sync.Mutex
	id       PageID
	size     int
	slots    []linePointer
	data     []byte
	upper    int // data area occupies data[upper:size]; grows downward
	stranded int // bytes of dead tuples whose slots were reused; compaction reclaims them
}

func newPage(id PageID, size int) *Page {
	return &Page{
		id:    id,
		size:  size,
		data:  make([]byte, size),
		upper: size,
	}
}

// lower is the end of the line-pointer area.
func (p *Page) lower() int {
	return pageHeaderSize + len(p.slots)*linePointerSize
}

// gapFree is the contiguous free space between line pointers and data.
func (p *Page) gapFree() int {
	return p.upper - p.lower()
}

// fragFree is the space reclaimable only by compaction: bytes held by dead
// tuples plus stranded bytes left behind when a dead slot was reused.
func (p *Page) fragFree() int {
	n := p.stranded
	for i := range p.slots {
		if p.slots[i].state == lpDead {
			n += p.slots[i].length
		}
	}
	return n
}

// totalFree is everything an insert could use: the gap plus fragments.
func (p *Page) totalFree() int {
	return p.gapFree() + p.fragFree()
}

// reusableSlot returns the first slot whose line pointer can be recycled
// for a new record, or -1. Reusing a slot never touches live records.
func (p *Page) reusableSlot() int {
	for i := range p.slots {
		if p.slots[i].state != lpLive {
			return i
		}
	}
	return -1
}

// place tries to put rec on the page. It reports whether it succeeded.
// The caller must hold p.mu.
//
// Decision order:
//  1. the contiguous gap fits — place directly;
//  2. gap + fragments fit — compact once, then place;
//  3. otherwise fail, so the caller can switch pages.
func (p *Page) place(rec []byte) (slot int, ok bool) {
	slot = p.reusableSlot()
	extra := 0
	if slot < 0 {
		extra = linePointerSize // a brand-new line pointer costs gap space
	}
	need := len(rec) + extra
	if p.gapFree() < need {
		if p.gapFree()+p.fragFree() < need {
			return -1, false
		}
		p.compact()
	}
	if slot < 0 {
		p.slots = append(p.slots, linePointer{state: lpUnused})
		slot = len(p.slots) - 1
	} else if p.slots[slot].state == lpDead {
		// Reusing a dead slot orphans its old tuple bytes; keep them on the
		// books as stranded space so compaction can reclaim them later.
		p.stranded += p.slots[slot].length
	}
	p.upper -= len(rec)
	copy(p.data[p.upper:p.upper+len(rec)], rec)
	p.slots[slot] = linePointer{state: lpLive, offset: p.upper, length: len(rec)}
	return slot, true
}

// compact reclaims dead tuples and slides live tuples to the end of the
// page, consolidating all free space into the gap. Slot numbers of live
// records are preserved; only their offsets change. Dead slots become
// unused (still reusable slot numbers). The caller must hold p.mu.
func (p *Page) compact() {
	type liveTuple struct {
		slot int
		buf  []byte
	}
	var live []liveTuple
	for i := range p.slots {
		lp := &p.slots[i]
		switch lp.state {
		case lpLive:
			buf := make([]byte, lp.length)
			copy(buf, p.data[lp.offset:lp.offset+lp.length])
			live = append(live, liveTuple{slot: i, buf: buf})
		case lpDead:
			*lp = linePointer{state: lpUnused}
		}
	}
	upper := p.size
	for _, lt := range live {
		upper -= len(lt.buf)
		copy(p.data[upper:upper+len(lt.buf)], lt.buf)
		p.slots[lt.slot].offset = upper
	}
	p.upper = upper
	p.stranded = 0
}

// get returns a copy of the live record in slot, or ok=false.
func (p *Page) get(slot int) (rec []byte, ok bool) {
	if slot < 0 || slot >= len(p.slots) || p.slots[slot].state != lpLive {
		return nil, false
	}
	lp := p.slots[slot]
	rec = make([]byte, lp.length)
	copy(rec, p.data[lp.offset:lp.offset+lp.length])
	return rec, true
}

// delete marks a live slot dead. The tuple bytes stay on the page as a
// fragment until a later compaction reclaims them.
func (p *Page) delete(slot int) bool {
	if slot < 0 || slot >= len(p.slots) || p.slots[slot].state != lpLive {
		return false
	}
	p.slots[slot].state = lpDead
	return true
}

// PageInfo is a snapshot of a page's real space accounting.
type PageInfo struct {
	ContigFree int // contiguous gap bytes
	FragFree   int // bytes reclaimable only by compaction (dead tuples + stranded)
	Live       int // live slots
	Dead       int // dead slots (fragments)
	Unused     int // unused slots (reusable)
	Slots      int // total line pointers
}

func (p *Page) info() PageInfo {
	var pi PageInfo
	pi.ContigFree = p.gapFree()
	pi.FragFree = p.stranded
	pi.Slots = len(p.slots)
	for i := range p.slots {
		switch p.slots[i].state {
		case lpLive:
			pi.Live++
		case lpDead:
			pi.Dead++
			pi.FragFree += p.slots[i].length
		case lpUnused:
			pi.Unused++
		}
	}
	return pi
}
