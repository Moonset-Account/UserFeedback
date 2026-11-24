//go:build unix

package blockcw

import (
	"context"
	"errors"
	"os"
	"syscall"
	"time"
)

// acquireFileLock acquires a BSD flock on a stable lock file. The state file
// itself is atomically renamed and therefore cannot serve as the stable lock
// inode. Separate open file descriptions are used for every acquisition, which
// also coordinates goroutines and multiple Store handles in one process.
func acquireFileLock(ctx context.Context, path string, exclusive bool) (func(), error) {
	how := syscall.LOCK_NB
	if exclusive {
		how |= syscall.LOCK_EX
	} else {
		how |= syscall.LOCK_SH
	}

	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()

	for {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
		if err != nil {
			return nil, err
		}

		if err := syscall.Flock(int(f.Fd()), how); err == nil {
			released := false
			release := func() {
				if released {
					return
				}
				released = true
				_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
				_ = f.Close()
			}
			return release, nil
		} else {
			_ = f.Close()
			if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EAGAIN) {
				return nil, err
			}
		}

		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.C:
		}
	}
}
