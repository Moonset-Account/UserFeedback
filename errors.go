package multipathwrite

import "errors"

// Sentinel errors returned by coordinator operations.
var (
	errInvalidOffset = errors.New("multipathwrite: offset must be non-negative")
	errEmptyWrite    = errors.New("multipathwrite: empty write")
	errInvalidRead   = errors.New("multipathwrite: invalid read parameters")
	errInvalidStatus = errors.New("multipathwrite: device reported an invalid status")
	errNotDurable    = errors.New("multipathwrite: write is not durable")
)
