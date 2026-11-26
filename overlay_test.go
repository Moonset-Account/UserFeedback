package overlay

import (
	"bytes"
	"errors"
	"io"
	"reflect"
	"sync"
	"testing"
	"time"
)

const testBS = 16 // backend block size used by the tests

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func readPath(t *testing.T, fs *FS, path string) string {
	t.Helper()
	f, err := fs.Open(path, O_RDONLY)
	must(t, err)
	defer f.Close()
	b, err := io.ReadAll(f)
	must(t, err)
	return string(b)
}

func readHandle(t *testing.T, f *File) string {
	t.Helper()
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	b, err := io.ReadAll(f)
	must(t, err)
	return string(b)
}

// blocks builds content with one distinct byte per block so partial
// copies are easy to recognise.
func blocks(bs string) []byte {
	var out []byte
	for i := 0; i < len(bs); i++ {
		out = append(out, bytes.Repeat([]byte{bs[i]}, testBS)...)
	}
	return out
}

// 正常场景：经任一名字首次写提升为一个上层对象，其余硬链接名与已打开
// 句柄读到同一新内容。
func TestPromoteSharesObjectAcrossNamesAndHandles(t *testing.T) {
	li := NewLowerImage()
	content := blocks("abc")
	must(t, li.AddFile("/dir/file", content))
	must(t, li.Link("/dir/file", "/dir/alias"))
	fs := New(li, NewMemBackend(testBS))

	// Handle opened before promotion, through the first name.
	h, err := fs.Open("/dir/file", O_RDONLY)
	must(t, err)

	// First write goes through the other name.
	w, err := fs.Open("/dir/alias", O_WRONLY)
	must(t, err)
	if _, err := w.Write([]byte("UPPER")); err != nil {
		t.Fatal(err)
	}
	must(t, w.Close())

	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d, want exactly 1", got)
	}

	want := "UPPER" + string(content[5:])
	if got := readPath(t, fs, "/dir/file"); got != want {
		t.Fatalf("/dir/file = %q, want %q", got, want)
	}
	if got := readPath(t, fs, "/dir/alias"); got != want {
		t.Fatalf("/dir/alias = %q, want %q", got, want)
	}
	if got := readHandle(t, h); got != want {
		t.Fatalf("pre-promotion handle reads %q, want %q", got, want)
	}
	must(t, h.Close())

	names, err := fs.ReadDir("/dir")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"alias", "file"}) {
		t.Fatalf("ReadDir(/dir) = %v", names)
	}
	if info, err := fs.Stat("/dir/file"); err != nil || info.Nlink != 2 {
		t.Fatalf("Stat nlink = %+v, %v; want nlink 2", info, err)
	}
}

// 并发边界：两个名字并发首次写，只能产生一次可见提升，且两个名字读到
// 一致内容。
func TestConcurrentFirstWritePromotesOnce(t *testing.T) {
	li := NewLowerImage()
	payload := blocks("abcdefgh")
	must(t, li.AddFile("/a", payload))
	must(t, li.Link("/a", "/b"))
	be := NewMemBackend(testBS)
	fs := New(li, be)

	// Pause the first promotion inside its first block write so the
	// second writer is certainly blocked in promote() at the same time.
	entered := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	be.SetHook(func(op Op, id uint64) error {
		if op == OpPut {
			once.Do(func() {
				close(entered)
				<-release
			})
		}
		return nil
	})

	p1 := bytes.Repeat([]byte("1"), len(payload))
	p2 := bytes.Repeat([]byte("2"), len(payload))

	errs := make(chan error, 2)
	writeVia := func(path string, data []byte) {
		f, err := fs.Open(path, O_WRONLY)
		if err == nil {
			_, err = f.Write(data)
			if cerr := f.Close(); err == nil {
				err = cerr
			}
		}
		errs <- err
	}
	go writeVia("/a", p1)
	<-entered             // first promotion is mid-copy
	go writeVia("/b", p2) // contends on the same object's promotion
	time.Sleep(50 * time.Millisecond)
	close(release)
	for i := 0; i < 2; i++ {
		if err := <-errs; err != nil {
			t.Fatalf("concurrent write: %v", err)
		}
	}
	be.SetHook(nil)

	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d, want exactly 1 visible promotion", got)
	}
	gotA := readPath(t, fs, "/a")
	if gotA != string(p1) && gotA != string(p2) {
		t.Fatalf("/a holds torn content %q...", gotA[:16])
	}
	if gotB := readPath(t, fs, "/b"); gotB != gotA {
		t.Fatalf("names disagree: /a=%q /b=%q", gotA[:16], gotB[:16])
	}
}

// 失败边界：复制做到一半失败时保留旧内容和全部可见名字，不泄漏块，
// 不记录提升，重试可成功。
func TestFailedCopyKeepsOldContentAndNames(t *testing.T) {
	li := NewLowerImage()
	content := blocks("xyz")
	must(t, li.AddFile("/x", content))
	must(t, li.Link("/x", "/y"))
	be := NewMemBackend(testBS)
	fs := New(li, be)

	errBoom := errors.New("injected backend failure")
	puts := 0
	be.SetHook(func(op Op, id uint64) error {
		if op == OpPut {
			puts++
			if puts == 2 { // fail the second of three copy-up blocks
				return errBoom
			}
		}
		return nil
	})

	f, err := fs.Open("/x", O_WRONLY)
	must(t, err)
	if _, err := f.Write([]byte("ZZ")); !errors.Is(err, errBoom) {
		t.Fatalf("Write err = %v, want injected failure", err)
	}
	must(t, f.Close())
	be.SetHook(nil)

	if got := fs.Promotions(); got != 0 {
		t.Fatalf("Promotions()=%d after failed copy, want 0", got)
	}
	if got := be.LiveBlocks(); got != 0 {
		t.Fatalf("LiveBlocks()=%d after failed copy, want 0 (leak)", got)
	}
	for _, p := range []string{"/x", "/y"} {
		if got := readPath(t, fs, p); got != string(content) {
			t.Fatalf("%s = %q, want original lower content", p, got)
		}
	}
	names, err := fs.ReadDir("/")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"x", "y"}) {
		t.Fatalf("ReadDir(/) = %v, want both names intact", names)
	}

	// The object is still lower-backed: a retry promotes cleanly and the
	// write lands on top of the copied old content.
	g, err := fs.Open("/y", O_WRONLY)
	must(t, err)
	if _, err := g.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	must(t, g.Close())
	want := "hello" + string(content[5:])
	if got := readPath(t, fs, "/x"); got != want {
		t.Fatalf("after retry /x = %q, want %q", got, want)
	}
	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d after retry, want 1", got)
	}
}

// 交错：目录在复制期间改名，提升按对象身份发布，旧路径不会把文件重新
// 建回来。
func TestRenameDirDuringCopyDoesNotRecreateOldPath(t *testing.T) {
	li := NewLowerImage()
	content := blocks("qrs")
	must(t, li.AddFile("/dir/file", content))
	be := NewMemBackend(testBS)
	fs := New(li, be)

	entered := make(chan struct{})
	release := make(chan struct{})
	puts := 0
	be.SetHook(func(op Op, id uint64) error {
		if op == OpPut {
			puts++
			if puts == 2 { // pause halfway through the copy-up
				close(entered)
				<-release
			}
		}
		return nil
	})

	errCh := make(chan error, 1)
	go func() {
		f, err := fs.Open("/dir/file", O_WRONLY)
		if err == nil {
			_, err = f.Write([]byte("NEW-CONTENT"))
			if cerr := f.Close(); err == nil {
				err = cerr
			}
		}
		errCh <- err
	}()

	<-entered // copy-up is paused mid-way
	must(t, fs.Rename("/dir", "/moved"))
	if _, err := fs.Stat("/dir/file"); err != ErrNotExist {
		t.Fatalf("old path during copy: err=%v, want ErrNotExist", err)
	}
	close(release)
	if err := <-errCh; err != nil {
		t.Fatalf("write through renamed-away path: %v", err)
	}
	be.SetHook(nil)

	// The completed promotion must not resurrect anything at the old path.
	if _, err := fs.Stat("/dir"); err != ErrNotExist {
		t.Fatalf("/dir after copy: err=%v, want ErrNotExist", err)
	}
	if _, err := fs.Stat("/dir/file"); err != ErrNotExist {
		t.Fatalf("/dir/file after copy: err=%v, want ErrNotExist", err)
	}
	// The write overwrote only the first 11 bytes of the promoted copy.
	want := "NEW-CONTENT" + string(content[11:])
	if got := readPath(t, fs, "/moved/file"); got != want {
		t.Fatalf("/moved/file = %q, want %q", got, want)
	}
	names, err := fs.ReadDir("/")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"moved"}) {
		t.Fatalf("ReadDir(/) = %v, want [moved]", names)
	}
	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d, want 1", got)
	}
}

// 回收时机：末名取消链接后对象仍被打开句柄钉住，末次关闭才回收并释放块。
func TestUnlinkReclaimsOnlyAfterLastClose(t *testing.T) {
	li := NewLowerImage()
	must(t, li.AddFile("/f", blocks("kl")))
	be := NewMemBackend(testBS)
	fs := New(li, be)

	f, err := fs.Open("/f", O_RDWR)
	must(t, err)
	if _, err := f.Write([]byte("NEW!")); err != nil {
		t.Fatal(err)
	}
	h, err := fs.Open("/f", O_RDONLY)
	must(t, err)

	must(t, fs.Unlink("/f"))
	if got := fs.Reclaimed(); got != 0 {
		t.Fatalf("Reclaimed()=%d after unlink with open handles, want 0", got)
	}
	if got := be.LiveBlocks(); got != 2 {
		t.Fatalf("LiveBlocks()=%d after unlink, want 2 (object pinned)", got)
	}
	if got := readHandle(t, f); got != "NEW!"+string(blocks("kl")[4:]) {
		t.Fatalf("unlinked handle reads %q", got)
	}

	must(t, f.Close()) // one handle remains
	if got := fs.Reclaimed(); got != 0 {
		t.Fatalf("Reclaimed()=%d with one handle left, want 0", got)
	}
	if got := be.LiveBlocks(); got != 2 {
		t.Fatalf("LiveBlocks()=%d with one handle left, want 2", got)
	}

	must(t, h.Close()) // last close reclaims
	if got := fs.Reclaimed(); got != 1 {
		t.Fatalf("Reclaimed()=%d after last close, want 1", got)
	}
	if got := be.LiveBlocks(); got != 0 {
		t.Fatalf("LiveBlocks()=%d after last close, want 0", got)
	}
	if _, err := fs.Stat("/f"); err != ErrNotExist {
		t.Fatalf("Stat(/f) err=%v, want ErrNotExist", err)
	}
}

// 故意交错：提升中途暂停，同时取消一个硬链接名并给目录改名，恢复后核对
// 目录项与内容，再经末次关闭核对回收时机。
func TestInterleavedPromoteRenameUnlinkLastClose(t *testing.T) {
	li := NewLowerImage()
	content := blocks("def")
	must(t, li.AddFile("/dir/file", content))
	must(t, li.Link("/dir/file", "/dir/link"))
	be := NewMemBackend(testBS)
	fs := New(li, be)

	// Handle on the second name, opened before anything happens.
	h, err := fs.Open("/dir/link", O_RDONLY)
	must(t, err)

	entered := make(chan struct{})
	release := make(chan struct{})
	puts := 0
	be.SetHook(func(op Op, id uint64) error {
		if op == OpPut {
			puts++
			if puts == 2 {
				close(entered)
				<-release
			}
		}
		return nil
	})

	errCh := make(chan error, 1)
	go func() {
		f, err := fs.Open("/dir/file", O_WRONLY)
		if err == nil {
			_, err = f.Write([]byte("W"))
			if cerr := f.Close(); err == nil {
				err = cerr
			}
		}
		errCh <- err
	}()

	<-entered // promotion paused mid-copy
	// Interleave: drop one hard-link name, then rename the directory.
	must(t, fs.Unlink("/dir/link"))
	must(t, fs.Rename("/dir", "/d2"))
	close(release)
	if err := <-errCh; err != nil {
		t.Fatalf("interleaved write: %v", err)
	}
	be.SetHook(nil)

	// Directory entries: old path gone, new path holds only "file".
	if _, err := fs.Stat("/dir"); err != ErrNotExist {
		t.Fatalf("/dir err=%v, want ErrNotExist", err)
	}
	names, err := fs.ReadDir("/")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"d2"}) {
		t.Fatalf("ReadDir(/) = %v, want [d2]", names)
	}
	names, err = fs.ReadDir("/d2")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"file"}) {
		t.Fatalf("ReadDir(/d2) = %v, want [file]", names)
	}

	// Content: promoted object visible via new path and the old handle.
	want := "W" + string(content[1:])
	if got := readPath(t, fs, "/d2/file"); got != want {
		t.Fatalf("/d2/file = %q, want %q", got, want)
	}
	if got := readHandle(t, h); got != want {
		t.Fatalf("handle on unlinked name reads %q, want %q", got, want)
	}
	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d, want 1", got)
	}

	// Reclaim timing: unlink the last name; the open handle still pins
	// the object; last close frees its blocks.
	must(t, fs.Unlink("/d2/file"))
	if got := fs.Reclaimed(); got != 0 {
		t.Fatalf("Reclaimed()=%d, want 0 while handle open", got)
	}
	if got := be.LiveBlocks(); got != 3 {
		t.Fatalf("LiveBlocks()=%d, want 3 while handle open", got)
	}
	if got := readHandle(t, h); got != want {
		t.Fatalf("pinned object reads %q, want %q", got, want)
	}
	must(t, h.Close())
	if got := fs.Reclaimed(); got != 1 {
		t.Fatalf("Reclaimed()=%d after last close, want 1", got)
	}
	if got := be.LiveBlocks(); got != 0 {
		t.Fatalf("LiveBlocks()=%d after last close, want 0", got)
	}
}

// 命名空间操作：覆盖层硬链接、跨目录改名、改名替换底层文件、白名单
// （whiteout）与重建、目录防护。
func TestNamespaceOps(t *testing.T) {
	li := NewLowerImage()
	must(t, li.AddFile("/a/f", []byte("aaa")))
	must(t, li.AddFile("/a/victim", []byte("vvv")))
	must(t, li.Mkdir("/a/sub"))
	must(t, li.Mkdir("/b"))
	fs := New(li, NewMemBackend(testBS))

	// Hard link created in the overlay shares the object.
	must(t, fs.Link("/a/f", "/b/g"))
	if info, err := fs.Stat("/b/g"); err != nil || info.Nlink != 2 {
		t.Fatalf("Stat(/b/g) = %+v, %v; want nlink 2", info, err)
	}
	w, err := fs.Open("/b/g", O_WRONLY)
	must(t, err)
	if _, err := w.Write([]byte("G")); err != nil {
		t.Fatal(err)
	}
	must(t, w.Close())
	if got := readPath(t, fs, "/a/f"); got != "Gaa" {
		t.Fatalf("/a/f = %q after write via /b/g, want Gaa", got)
	}

	// Rename across directories.
	must(t, fs.Rename("/b/g", "/b/h"))
	if got := readPath(t, fs, "/b/h"); got != "Gaa" {
		t.Fatalf("/b/h = %q, want Gaa", got)
	}
	if _, err := fs.Stat("/b/g"); err != ErrNotExist {
		t.Fatalf("/b/g err=%v, want ErrNotExist", err)
	}

	// Rename over an existing lower file replaces and reclaims it.
	must(t, fs.Rename("/b/h", "/a/victim"))
	if got := readPath(t, fs, "/a/victim"); got != "Gaa" {
		t.Fatalf("/a/victim = %q, want Gaa", got)
	}
	if got := fs.Reclaimed(); got != 1 {
		t.Fatalf("Reclaimed()=%d, want 1 (replaced victim)", got)
	}

	// Unlink whites out the lower name; O_CREATE replaces it.
	must(t, fs.Unlink("/a/victim"))
	names, err := fs.ReadDir("/a")
	must(t, err)
	if !reflect.DeepEqual(names, []string{"f", "sub"}) {
		t.Fatalf("ReadDir(/a) = %v", names)
	}
	c, err := fs.Open("/a/victim", O_CREATE|O_RDWR)
	must(t, err)
	if _, err := c.Write([]byte("new")); err != nil {
		t.Fatal(err)
	}
	must(t, c.Close())
	if got := readPath(t, fs, "/a/victim"); got != "new" {
		t.Fatalf("recreated /a/victim = %q, want new", got)
	}

	// Mkdir over a whited-out name; unlinking a directory is rejected.
	must(t, fs.Unlink("/a/victim"))
	must(t, fs.Mkdir("/a/victim"))
	if info, err := fs.Stat("/a/victim"); err != nil || !info.IsDir {
		t.Fatalf("Stat(/a/victim) = %+v, %v; want dir", info, err)
	}
	if err := fs.Unlink("/a/sub"); err != ErrIsDir {
		t.Fatalf("Unlink(dir) err=%v, want ErrIsDir", err)
	}

	// A directory cannot be moved into its own subtree.
	if err := fs.Rename("/a", "/a/sub/x"); err != ErrInvalid {
		t.Fatalf("Rename into subtree err=%v, want ErrInvalid", err)
	}
	// Directory replacement is rejected.
	if err := fs.Rename("/a/sub", "/a/victim"); err != ErrExist {
		t.Fatalf("Rename dir over dir err=%v, want ErrExist", err)
	}
}

// O_CREATE 与 O_TRUNC 行为。
func TestOpenCreateAndTruncate(t *testing.T) {
	li := NewLowerImage()
	must(t, li.AddFile("/f", []byte("abcdef")))
	fs := New(li, NewMemBackend(testBS))

	c, err := fs.Open("/new", O_CREATE|O_RDWR)
	must(t, err)
	if _, err := c.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	if got := readHandle(t, c); got != "hello" {
		t.Fatalf("new file reads %q, want hello", got)
	}
	must(t, c.Close())
	if info, err := fs.Stat("/new"); err != nil || info.Size != 5 {
		t.Fatalf("Stat(/new) = %+v, %v; want size 5", info, err)
	}

	// O_TRUNC promotes then empties the file.
	g, err := fs.Open("/f", O_WRONLY|O_TRUNC)
	must(t, err)
	if info, err := fs.Stat("/f"); err != nil || info.Size != 0 {
		t.Fatalf("Stat(/f) after O_TRUNC = %+v, %v; want size 0", info, err)
	}
	if _, err := g.Write([]byte("xy")); err != nil {
		t.Fatal(err)
	}
	must(t, g.Close())
	if got := readPath(t, fs, "/f"); got != "xy" {
		t.Fatalf("/f = %q after truncate+write, want xy", got)
	}
	if got := fs.Promotions(); got != 1 {
		t.Fatalf("Promotions()=%d, want 1 (truncate promoted /f)", got)
	}

	// Opening a directory or a bad flag combination fails cleanly.
	if _, err := fs.Open("/", O_RDONLY); err != ErrIsDir {
		t.Fatalf("Open(/) err=%v, want ErrIsDir", err)
	}
	if _, err := fs.Open("/missing", O_RDONLY); err != ErrNotExist {
		t.Fatalf("Open(/missing) err=%v, want ErrNotExist", err)
	}
	r, err := fs.Open("/f", O_WRONLY)
	must(t, err)
	if _, err := r.Read(make([]byte, 1)); err != ErrBadFD {
		t.Fatalf("Read on O_WRONLY err=%v, want ErrBadFD", err)
	}
	must(t, r.Close())
}
