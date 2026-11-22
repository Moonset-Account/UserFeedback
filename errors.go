package txstore

import "errors"

// Sentinel errors. Errors returned by the package wrap these values, so use
// errors.Is to classify them.
var (
	// ErrUniqueViolation means two rows with the same non-NULL composite key
	// would be visible together.
	ErrUniqueViolation = errors.New("txstore: unique violation")
	// ErrWriteConflict means a row written by the transaction changed in the
	// committed state after the transaction's snapshot was taken.
	ErrWriteConflict = errors.New("txstore: write conflict")
	// ErrRowExists means Insert was asked to create an ID already visible in
	// the transaction's read view.
	ErrRowExists = errors.New("txstore: row already exists")
	// ErrRowNotFound means Update or Delete targeted an ID not visible in the
	// transaction's read view.
	ErrRowNotFound = errors.New("txstore: row not found")
	// ErrDuplicateID means Open received the same row ID more than once.
	ErrDuplicateID = errors.New("txstore: duplicate row ID")
	// ErrSavepointNotFound means the named savepoint is absent or has been
	// invalidated by a rollback to an earlier savepoint.
	ErrSavepointNotFound = errors.New("txstore: savepoint not found")
	// ErrSavepointExists means an active savepoint already uses the name.
	ErrSavepointExists = errors.New("txstore: savepoint already exists")
	// ErrInvalidSavepointName means the savepoint name was empty.
	ErrInvalidSavepointName = errors.New("txstore: invalid savepoint name")
	// ErrInvalidMode means SetMode received a mode other than ModeStatement
	// or ModeDeferred.
	ErrInvalidMode = errors.New("txstore: invalid constraint mode")
	// ErrTxnClosed means the transaction was already committed or rolled back.
	ErrTxnClosed = errors.New("txstore: transaction closed")
)
