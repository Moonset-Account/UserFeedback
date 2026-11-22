// Package txstore is an in-memory transaction layer with savepoints.
//
// Rows carry a string ID and a nullable composite unique key (Key). A key
// containing at least one NULL column is exempt from the unique constraint,
// mirroring SQL NULL semantics: many such rows may share a key.
//
// Transactions read a stable snapshot taken at Begin and see their own
// writes. Unique-key checking can be switched per transaction between
// ModeStatement (check after every mutation) and ModeDeferred (check at
// Commit), which allows writing two rows to the same key before resolving it,
// such as swapping two keys. Named savepoints capture both the row edits and
// the current mode; rolling back to a savepoint restores both and invalidates
// savepoints created after it.
//
// Commit validates against the latest committed state: rows this transaction
// did not modify must still match the snapshot, a concurrent rewrite of a row
// this transaction did modify is a write conflict, and the final key set must
// satisfy the unique constraint. Validation happens before any write, so a
// failed commit never leaves a partial key change.
//
// The Store is safe for concurrent use; a Txn is meant for a single goroutine.
package txstore
