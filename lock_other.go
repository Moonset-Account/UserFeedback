//go:build !unix

package blockcw

import "context"

// acquireFileLock is a no-op fallback for non-Unix platforms in this
// simulator. Durable atomic state replacement still works across restarts, but
// concurrent processes opening the same state path are not coordinated by this
// fallback. The test and verification environment is Unix/macOS, where flock is
// used.
func acquireFileLock(ctx context.Context, path string, exclusive bool) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return func() {}, nil
}
