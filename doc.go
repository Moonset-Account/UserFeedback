// Package warmgrid is an in-memory spatial index for road/route geometries on
// an integer coordinate plane partitioned into a half-open grid.
//
// # Storage model
//
// Each geometry is stored exactly once as a main record. A geometry whose
// bounding box touches several grid cells gets one lightweight index term
// (record id + version) in every covered cell. A query gathers the terms from
// all cells covered by its box, deduplicates them by (id, version), and then
// fetches each main record once to run exact bounding-box and boundary-relation
// filtering.
//
// # Half-open grid
//
// Cell (x, y) of a grid of size S covers [x*S, (x+1)*S) x [y*S, (y+1)*S). A
// coordinate exactly on a cell line therefore belongs to the cell on its
// positive side: on a size-10 grid the point (10, 10) is in cell (1, 1), and the
// closed box [0,10]x[0,10] covers all four cells around that corner. Indexing
// and querying use the same rule, so a route that lies exactly on a cell line is
// found by a query whose bounds only reach the line, and never by a query that
// stops short of it. Query boxes are closed: an edge point counts as inside.
// RelationTouches/resp. RelationInside keep that same convention.
//
// # Snapshot isolation and local redraw
//
// Index publishes immutable snapshots. Update and Apply use copy-on-write: only
// the cell buckets on the union of the old and new cover sets are rebuilt; every
// other bucket is shared with the previous snapshot. A Snapshot obtained before
// an update keeps observing the old record version and deduplicates it across
// all of its old cells, while a Snapshot obtained afterwards immediately sees
// the new cover cells. Because a snapshot is a single immutable object, one
// query can never mix two versions of the same record.
package warmgrid
