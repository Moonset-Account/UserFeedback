package rootfs_test

import (
	"errors"
	"reflect"
	"testing"

	"rootfs"
)

func mustBuild(t *testing.T, layers ...rootfs.Layer) *rootfs.Image {
	t.Helper()
	img, err := rootfs.Build(layers)
	if err != nil {
		t.Fatalf("Build: unexpected error: %v", err)
	}
	return img
}

func readString(t *testing.T, img *rootfs.Image, layer int, p string) string {
	t.Helper()
	fc, err := img.ReadFile(layer, p)
	if err != nil {
		t.Fatalf("ReadFile(%d, %q): unexpected error: %v", layer, p, err)
	}
	return string(fc.Data)
}

func names(entries []rootfs.DirEntry) []string {
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = e.Name
	}
	return out
}

// baseLayers reproduces the prompt's scenario: a base layer that ships two
// configs under /etc/app, then a layer that marks that directory opaque and
// drops in a fresh config of the same name.
func baseLayers() []rootfs.Layer {
	return []rootfs.Layer{
		{Entries: []rootfs.Entry{
			rootfs.Dir("/etc"),
			rootfs.Dir("/etc/app"),
			rootfs.File("/etc/app/app.yaml", []byte("release: old\n")),
			rootfs.File("/etc/app/logging.conf", []byte("level: info\n")),
			rootfs.File("/etc/hostname", []byte("base\n")),
		}},
		{Entries: []rootfs.Entry{
			rootfs.Opaque("/etc/app"),
			rootfs.File("/etc/app/app.yaml", []byte("release: new\n")),
		}},
	}
}

func TestNormalScenarioOpaqueReplacesInheritedChildren(t *testing.T) {
	img := mustBuild(t, baseLayers()...)

	if got, want := img.NumLayers(), 2; got != want {
		t.Fatalf("NumLayers() = %d, want %d", got, want)
	}

	fc, err := img.ReadFile(1, "/etc/app/app.yaml")
	if err != nil {
		t.Fatalf("ReadFile newest: %v", err)
	}
	if string(fc.Data) != "release: new\n" {
		t.Fatalf("newest app.yaml = %q, want %q", fc.Data, "release: new\n")
	}
	if fc.Layer != 1 {
		t.Fatalf("newest app.yaml Layer = %d, want 1", fc.Layer)
	}
	if fc.Size != len("release: new\n") {
		t.Fatalf("newest app.yaml Size = %d, want %d", fc.Size, len("release: new\n"))
	}

	entries, err := img.ListDir(1, "/etc/app")
	if err != nil {
		t.Fatalf("ListDir newest: %v", err)
	}
	if got, want := names(entries), []string{"app.yaml"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("newest /etc/app children = %v, want %v", got, want)
	}
	if entries[0].Layer != 1 || entries[0].IsDir || entries[0].Size != len("release: new\n") {
		t.Fatalf("newest app.yaml entry = %+v, want layer 1 file of size %d", entries[0], len("release: new\n"))
	}

	// The opaque marker hid logging.conf, which only existed in layer 0.
	if _, err := img.ReadFile(1, "/etc/app/logging.conf"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("logging.conf at layer 1: err = %v, want ErrNotExist", err)
	}
	if _, err := img.Stat(1, "/etc/app/logging.conf"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("Stat logging.conf at layer 1: err = %v, want ErrNotExist", err)
	}

	// The opaque marker hid inherited children only: the directory itself and
	// the sibling file outside it survive.
	fi, err := img.Stat(1, "/etc/app")
	if err != nil {
		t.Fatalf("Stat /etc/app: %v", err)
	}
	if !fi.IsDir {
		t.Fatalf("/etc/app IsDir = false, want true")
	}
	if got := readString(t, img, 1, "/etc/hostname"); got != "base\n" {
		t.Fatalf("hostname at layer 1 = %q, want %q", got, "base\n")
	}

	// Asking about the earlier layer still shows the original configuration.
	fc, err = img.ReadFile(0, "/etc/app/app.yaml")
	if err != nil {
		t.Fatalf("ReadFile layer 0: %v", err)
	}
	if string(fc.Data) != "release: old\n" || fc.Layer != 0 {
		t.Fatalf("layer 0 app.yaml = %q from layer %d, want %q from layer 0", fc.Data, fc.Layer, "release: old\n")
	}
	if got := readString(t, img, 0, "/etc/app/logging.conf"); got != "level: info\n" {
		t.Fatalf("layer 0 logging.conf = %q, want %q", got, "level: info\n")
	}
	if got, want := names(mustList(t, img, 0, "/etc/app")), []string{"app.yaml", "logging.conf"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("layer 0 /etc/app children = %v, want %v", got, want)
	}
}

func mustList(t *testing.T, img *rootfs.Image, layer int, p string) []rootfs.DirEntry {
	t.Helper()
	entries, err := img.ListDir(layer, p)
	if err != nil {
		t.Fatalf("ListDir(%d, %q): %v", layer, p, err)
	}
	return entries
}

func TestWhiteoutHidesSubtree(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/srv"),
			rootfs.Dir("/srv/www"),
			rootfs.File("/srv/www/index.html", []byte("<h1>base</h1>")),
			rootfs.File("/srv/www/assets/app.css", []byte("body{}")),
			rootfs.File("/srv/keep.txt", []byte("keep")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Whiteout("/srv/www"),
		}},
	)

	if _, err := img.Stat(1, "/srv/www"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("Stat /srv/www after whiteout: err = %v, want ErrNotExist", err)
	}
	if _, err := img.ReadFile(1, "/srv/www/index.html"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("read descendant of whited-out dir: err = %v, want ErrNotExist", err)
	}
	if _, err := img.Stat(1, "/srv/www/assets/app.css"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("stat nested descendant of whited-out dir: err = %v, want ErrNotExist", err)
	}
	if got, want := names(mustList(t, img, 1, "/srv")), []string{"keep.txt"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("/srv children after whiteout = %v, want %v", got, want)
	}

	// The earlier layer is untouched by the marker.
	if got := readString(t, img, 0, "/srv/www/index.html"); got != "<h1>base</h1>" {
		t.Fatalf("layer 0 index.html = %q, want %q", got, "<h1>base</h1>")
	}
}

func TestSameLayerContentSurvivesItsOwnMarkers(t *testing.T) {
	// Layer 1 whited out the old file and wrote a replacement, plus marked
	// /etc/app opaque while adding its own child. Order in Entries is shuffled
	// on purpose: nothing here should depend on it.
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/app/app.yaml", []byte("old")),
			rootfs.File("/etc/app/legacy.conf", []byte("legacy")),
			rootfs.File("/etc/other", []byte("other")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/app/app.yaml", []byte("new")),
			rootfs.Opaque("/etc/app"),
			rootfs.Whiteout("/etc/app/app.yaml"),
			rootfs.Whiteout("/etc/other"),
			rootfs.File("/etc/app/extra.conf", []byte("extra")),
		}},
	)

	// The whiteout names a path the same layer re-added: content wins.
	if got := readString(t, img, 1, "/etc/app/app.yaml"); got != "new" {
		t.Fatalf("app.yaml at layer 1 = %q, want %q", got, "new")
	}
	if got := readString(t, img, 1, "/etc/app/extra.conf"); got != "extra" {
		t.Fatalf("extra.conf at layer 1 = %q, want %q", got, "extra")
	}
	if _, err := img.ReadFile(1, "/etc/app/legacy.conf"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("legacy.conf at layer 1: err = %v, want ErrNotExist", err)
	}
	if _, err := img.ReadFile(1, "/etc/other"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("/etc/other at layer 1: err = %v, want ErrNotExist", err)
	}

	// Layer 0 must be wholly unaffected by layer 1's markers and rewrites.
	if got := readString(t, img, 0, "/etc/app/app.yaml"); got != "old" {
		t.Fatalf("layer 0 app.yaml = %q, want %q", got, "old")
	}
	if got := readString(t, img, 0, "/etc/app/legacy.conf"); got != "legacy" {
		t.Fatalf("layer 0 legacy.conf = %q, want %q", got, "legacy")
	}
	if got := readString(t, img, 0, "/etc/other"); got != "other" {
		t.Fatalf("layer 0 /etc/other = %q, want %q", got, "other")
	}
}

func TestEntryOrderDoesNotMatter(t *testing.T) {
	base := rootfs.Layer{Entries: []rootfs.Entry{
		rootfs.Dir("/a/b"),
		rootfs.File("/a/b/inherited", []byte("old")),
	}}
	entries := []rootfs.Entry{
		rootfs.Dir("/a/b"),
		rootfs.Opaque("/a/b"),
		rootfs.Whiteout("/a/b/inherited"),
		rootfs.File("/a/b/new1", []byte("one")),
		rootfs.File("/a/b/new2", []byte("two")),
		rootfs.Whiteout("/a/gone"),
	}
	reversed := make([]rootfs.Entry, len(entries))
	for i := range entries {
		reversed[len(entries)-1-i] = entries[i]
	}

	imgA := mustBuild(t, base, rootfs.Layer{Entries: entries})
	imgB := mustBuild(t, base, rootfs.Layer{Entries: reversed})

	wantChildren := []string{"new1", "new2"}
	for name, img := range map[string]*rootfs.Image{"forward": imgA, "reversed": imgB} {
		if got := names(mustList(t, img, 1, "/a/b")); !reflect.DeepEqual(got, wantChildren) {
			t.Fatalf("%s: /a/b children = %v, want %v", name, got, wantChildren)
		}
		for p, want := range map[string]string{"/a/b/new1": "one", "/a/b/new2": "two"} {
			if got := readString(t, img, 1, p); got != want {
				t.Fatalf("%s: %s = %q, want %q", name, p, got, want)
			}
		}
		if _, err := img.Stat(1, "/a/b/inherited"); !errors.Is(err, rootfs.ErrNotExist) {
			t.Fatalf("%s: inherited: err = %v, want ErrNotExist", name, err)
		}
		if _, err := img.Stat(1, "/a/gone"); !errors.Is(err, rootfs.ErrNotExist) {
			t.Fatalf("%s: /a/gone: err = %v, want ErrNotExist", name, err)
		}
	}
}

func TestFileReplacingDirectoryDropsDescendants(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/var/lib/app"),
			rootfs.File("/var/lib/app/data.bin", []byte("payload")),
			rootfs.File("/var/lib/app/nested/deep.txt", []byte("deep")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/var/lib/app", []byte("now a file")),
		}},
	)

	fc, err := img.ReadFile(1, "/var/lib/app")
	if err != nil {
		t.Fatalf("ReadFile replacement: %v", err)
	}
	if string(fc.Data) != "now a file" || fc.Layer != 1 {
		t.Fatalf("replacement = %q from layer %d, want %q from layer 1", fc.Data, fc.Layer, "now a file")
	}
	if _, err := img.Stat(1, "/var/lib/app/data.bin"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("old descendant survives: err = %v, want ErrNotExist", err)
	}
	if _, err := img.Stat(1, "/var/lib/app/nested/deep.txt"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("old nested descendant survives: err = %v, want ErrNotExist", err)
	}
	if _, err := img.ListDir(1, "/var/lib/app"); !errors.Is(err, rootfs.ErrNotDir) {
		t.Fatalf("ListDir on replacement file: err = %v, want ErrNotDir", err)
	}
	if _, err := img.ReadFile(1, "/var/lib/app/data.bin"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("ReadFile through a file ancestor: err = %v, want ErrNotExist", err)
	}

	// Layer 0 still has the directory tree.
	if got := readString(t, img, 0, "/var/lib/app/data.bin"); got != "payload" {
		t.Fatalf("layer 0 data.bin = %q, want %q", got, "payload")
	}
}

func TestDirectoryReplacingFile(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/opt/tool", []byte("binary")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/opt/tool"),
			rootfs.File("/opt/tool/README", []byte("docs")),
		}},
	)

	fi, err := img.Stat(1, "/opt/tool")
	if err != nil {
		t.Fatalf("Stat /opt/tool: %v", err)
	}
	if !fi.IsDir {
		t.Fatalf("/opt/tool is not a directory after replacement")
	}
	if got := readString(t, img, 1, "/opt/tool/README"); got != "docs" {
		t.Fatalf("README = %q, want %q", got, "docs")
	}
	if got, want := names(mustList(t, img, 1, "/opt/tool")), []string{"README"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("/opt/tool children = %v, want %v", got, want)
	}
}

func TestLayerProvenanceThroughStack(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/motd", []byte("v1")),
			rootfs.File("/etc/version", []byte("1.0")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/motd", []byte("v2")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/motd", []byte("v3")),
		}},
	)

	if got, want := img.NumLayers(), 3; got != want {
		t.Fatalf("NumLayers() = %d, want %d", got, want)
	}
	for layer, want := range map[int]string{0: "v1", 1: "v2", 2: "v3"} {
		fc, err := img.ReadFile(layer, "/etc/motd")
		if err != nil {
			t.Fatalf("ReadFile(%d): %v", layer, err)
		}
		if string(fc.Data) != want {
			t.Fatalf("layer %d motd = %q, want %q", layer, fc.Data, want)
		}
		if fc.Layer != layer {
			t.Fatalf("layer %d motd provenance = %d, want %d", layer, fc.Layer, layer)
		}
	}

	fi, err := img.Stat(2, "/etc/version")
	if err != nil {
		t.Fatalf("Stat /etc/version: %v", err)
	}
	if fi.Layer != 0 || fi.Size != len("1.0") {
		t.Fatalf("/etc/version = %+v, want layer 0 size %d", fi, len("1.0"))
	}

	rootEntries := mustList(t, img, 2, "/")
	if names := names(rootEntries); len(names) != 1 || names[0] != "etc" {
		t.Fatalf("root children = %v, want [etc]", names)
	}
	// The root itself is synthetic, so its origin is -1.
	if r, err := img.Stat(2, "/"); err != nil || r.Layer != -1 {
		t.Fatalf("Stat / = %+v, err = %v; want layer -1", r, err)
	}
}

func TestOpaqueDirectoryCreatedOnlyByMarker(t *testing.T) {
	// An opaque marker on a path with no inherited directory and no explicit
	// directory record still establishes an empty directory.
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/app/app.yaml", []byte("old")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Opaque("/etc/app"),
		}},
	)

	fi, err := img.Stat(1, "/etc/app")
	if err != nil {
		t.Fatalf("Stat /etc/app: %v", err)
	}
	if !fi.IsDir {
		t.Fatalf("/etc/app IsDir = false, want true")
	}
	if got := mustList(t, img, 1, "/etc/app"); len(got) != 0 {
		t.Fatalf("/etc/app children = %v, want none", names(got))
	}
	if _, err := img.ReadFile(1, "/etc/app/app.yaml"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("app.yaml after opaque: err = %v, want ErrNotExist", err)
	}
}

func TestOpaqueDoesNotHideSameLayerChildren(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/etc/app/inherited.conf", []byte("old")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Opaque("/etc/app"),
			rootfs.File("/etc/app/own.conf", []byte("new")),
			rootfs.Dir("/etc/app/subdir"),
			rootfs.File("/etc/app/subdir/file", []byte("leaf")),
		}},
	)

	if got, want := names(mustList(t, img, 1, "/etc/app")), []string{"own.conf", "subdir"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("/etc/app children = %v, want %v", got, want)
	}
	if got := readString(t, img, 1, "/etc/app/subdir/file"); got != "leaf" {
		t.Fatalf("subdir/file = %q, want %q", got, "leaf")
	}
}

func TestWhiteoutThenRecreatedByLaterLayer(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/srv/www/index.html", []byte("v1")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Whiteout("/srv/www"),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/srv/www"),
			rootfs.File("/srv/www/index.html", []byte("v3")),
		}},
	)

	if _, err := img.Stat(1, "/srv/www"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("layer 1 /srv/www: err = %v, want ErrNotExist", err)
	}
	if got := readString(t, img, 2, "/srv/www/index.html"); got != "v3" {
		t.Fatalf("layer 2 index.html = %q, want %q", got, "v3")
	}
	// The recreation is a new item: its origin is the recreating layer.
	fi, err := img.Stat(2, "/srv/www/index.html")
	if err != nil {
		t.Fatalf("Stat layer 2: %v", err)
	}
	if fi.Layer != 2 {
		t.Fatalf("layer 2 index.html origin = %d, want 2", fi.Layer)
	}
	if got := readString(t, img, 0, "/srv/www/index.html"); got != "v1" {
		t.Fatalf("layer 0 index.html = %q, want %q", got, "v1")
	}
}

func TestDirectoryProvenance(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/etc/app"),
			rootfs.File("/etc/app/app.yaml", []byte("old")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Dir("/etc/app"),
			rootfs.File("/etc/app/app.yaml", []byte("new")),
			rootfs.File("/var/log/app.log", []byte("log")),
		}},
	)

	// An explicit directory record in layer 1 re-origins the directory itself
	// while its inherited children keep their own origins.
	fi, err := img.Stat(1, "/etc/app")
	if err != nil {
		t.Fatalf("Stat /etc/app: %v", err)
	}
	if fi.Layer != 1 || !fi.IsDir {
		t.Fatalf("/etc/app = %+v, want layer 1 directory", fi)
	}

	// Ancestors created implicitly to hold a file originate at that file's
	// layer.
	for _, p := range []string{"/var", "/var/log"} {
		fi, err := img.Stat(1, p)
		if err != nil {
			t.Fatalf("Stat %s: %v", p, err)
		}
		if fi.Layer != 1 || !fi.IsDir {
			t.Fatalf("%s = %+v, want layer 1 directory", p, fi)
		}
	}
	entries := mustList(t, img, 1, "/var/log")
	if len(entries) != 1 || entries[0].Name != "app.log" || entries[0].Layer != 1 {
		t.Fatalf("/var/log entries = %+v, want app.log from layer 1", entries)
	}

	// At layer 0 the implicitly created ancestors do not exist yet.
	img0 := mustBuild(t, rootfs.Layer{Entries: []rootfs.Entry{
		rootfs.File("/var/log/app.log", []byte("log")),
	}})
	fi, err = img0.Stat(0, "/var")
	if err != nil {
		t.Fatalf("Stat /var: %v", err)
	}
	if fi.Layer != 0 {
		t.Fatalf("/var origin = %d, want 0", fi.Layer)
	}
}

func TestWhiteoutOfParentWithSameLayerChild(t *testing.T) {
	// The whiteout hides only the inherited tree; content this layer places
	// below the same path is kept.
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/srv/www/old.html", []byte("old")),
			rootfs.File("/srv/other.txt", []byte("other")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/srv/www/new.html", []byte("new")),
			rootfs.Whiteout("/srv/www"),
		}},
	)

	if _, err := img.Stat(1, "/srv/www/old.html"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("old.html: err = %v, want ErrNotExist", err)
	}
	if got := readString(t, img, 1, "/srv/www/new.html"); got != "new" {
		t.Fatalf("new.html = %q, want %q", got, "new")
	}
	if got, want := names(mustList(t, img, 1, "/srv/www")), []string{"new.html"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("/srv/www children = %v, want %v", got, want)
	}
	if got := readString(t, img, 0, "/srv/www/old.html"); got != "old" {
		t.Fatalf("layer 0 old.html = %q, want %q", got, "old")
	}
}

func TestOpaqueOverInheritedFileBecomesDirectory(t *testing.T) {
	img := mustBuild(t,
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.File("/data/cache", []byte("was a file")),
		}},
		rootfs.Layer{Entries: []rootfs.Entry{
			rootfs.Opaque("/data/cache"),
		}},
	)

	fi, err := img.Stat(1, "/data/cache")
	if err != nil {
		t.Fatalf("Stat /data/cache: %v", err)
	}
	if !fi.IsDir {
		t.Fatalf("/data/cache = %+v, want directory after opaque marker", fi)
	}
	if got := mustList(t, img, 1, "/data/cache"); len(got) != 0 {
		t.Fatalf("/data/cache children = %v, want none", names(got))
	}
	if got := readString(t, img, 0, "/data/cache"); got != "was a file" {
		t.Fatalf("layer 0 /data/cache = %q, want %q", got, "was a file")
	}
}

func TestQueryErrors(t *testing.T) {
	img := mustBuild(t, baseLayers()...)

	if _, err := img.ReadFile(2, "/etc/hostname"); !errors.Is(err, rootfs.ErrLayerOutOfRange) {
		t.Fatalf("layer 2: err = %v, want ErrLayerOutOfRange", err)
	}
	if _, err := img.ReadFile(-1, "/etc/hostname"); !errors.Is(err, rootfs.ErrLayerOutOfRange) {
		t.Fatalf("layer -1: err = %v, want ErrLayerOutOfRange", err)
	}
	if _, err := img.ListDir(0, "/nope"); !errors.Is(err, rootfs.ErrNotExist) {
		t.Fatalf("ListDir missing: err = %v, want ErrNotExist", err)
	}
	if _, err := img.ListDir(0, "/etc/hostname"); !errors.Is(err, rootfs.ErrNotDir) {
		t.Fatalf("ListDir on file: err = %v, want ErrNotDir", err)
	}
	if _, err := img.ReadFile(0, "/etc"); !errors.Is(err, rootfs.ErrIsDir) {
		t.Fatalf("ReadFile on dir: err = %v, want ErrIsDir", err)
	}
	if _, err := img.ReadFile(0, "../escape"); !errors.Is(err, rootfs.ErrInvalidPath) {
		t.Fatalf("read ..: err = %v, want ErrInvalidPath", err)
	}
	if _, err := img.Stat(0, ""); !errors.Is(err, rootfs.ErrInvalidPath) {
		t.Fatalf("stat empty: err = %v, want ErrInvalidPath", err)
	}
}

func TestPathsAreNormalized(t *testing.T) {
	// Two records that normalize to the same canonical path are contradictory
	// in one layer, so Build must reject them rather than silently pick one.
	_, err := rootfs.Build([]rootfs.Layer{{Entries: []rootfs.Entry{
		rootfs.File("/etc/app.yaml", []byte("a")),
		rootfs.File("etc/app.yaml", []byte("b")),
	}}})
	if !errors.Is(err, rootfs.ErrDuplicatePath) {
		t.Fatalf("duplicate canonical paths: err = %v, want ErrDuplicatePath", err)
	}

	// A relative entry and a query written with redundant separators and "."
	// segments resolve to the same canonical path.
	img := mustBuild(t, rootfs.Layer{Entries: []rootfs.Entry{
		rootfs.File("etc/app.yaml", []byte("ok")),
	}})
	for _, p := range []string{"etc/app.yaml", "/etc/app.yaml", "//etc//app.yaml", "/etc/./app.yaml"} {
		fc, err := img.ReadFile(0, p)
		if err != nil {
			t.Fatalf("ReadFile(%q): %v", p, err)
		}
		if string(fc.Data) != "ok" || fc.Path != "/etc/app.yaml" {
			t.Fatalf("ReadFile(%q) = %q @ %q, want ok @ /etc/app.yaml", p, fc.Data, fc.Path)
		}
	}
}

func TestBuildRejectsBadInput(t *testing.T) {
	tests := []struct {
		name   string
		layer  rootfs.Layer
		target error
	}{
		{
			name:   "root entry",
			layer:  rootfs.Layer{Entries: []rootfs.Entry{rootfs.Dir("/")}},
			target: rootfs.ErrInvalidEntry,
		},
		{
			name: "directory with content",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				{Path: "/d", Kind: rootfs.KindDir, Content: []byte("x")},
			}},
			target: rootfs.ErrInvalidEntry,
		},
		{
			name: "unknown kind",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				{Path: "/x", Kind: rootfs.Kind(99)},
			}},
			target: rootfs.ErrInvalidEntry,
		},
		{
			name: "escape above root",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("../x", []byte("x")),
			}},
			target: rootfs.ErrInvalidPath,
		},
		{
			name: "duplicate file",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("/a", []byte("1")),
				rootfs.File("/a", []byte("2")),
			}},
			target: rootfs.ErrDuplicatePath,
		},
		{
			name: "file and dir same path",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("/a", []byte("1")),
				rootfs.Dir("/a"),
			}},
			target: rootfs.ErrConflict,
		},
		{
			name: "file as ancestor",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("/a", []byte("1")),
				rootfs.File("/a/b", []byte("2")),
			}},
			target: rootfs.ErrConflict,
		},
		{
			name: "whiteout below same-layer file",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("/a", []byte("1")),
				rootfs.Whiteout("/a/b"),
			}},
			target: rootfs.ErrConflict,
		},
		{
			name: "opaque below same-layer file",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.File("/a", []byte("1")),
				rootfs.Opaque("/a/dir"),
			}},
			target: rootfs.ErrConflict,
		},
		{
			name: "whiteout and file same path",
			layer: rootfs.Layer{Entries: []rootfs.Entry{
				rootfs.Whiteout("/a"),
				rootfs.File("/a", []byte("1")),
			}},
			target: nil, // allowed: whiteout hides the lower item, the file replaces it
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := rootfs.Build([]rootfs.Layer{tc.layer})
			if tc.target == nil {
				if err != nil {
					t.Fatalf("Build error = %v, want success", err)
				}
				return
			}
			if !errors.Is(err, tc.target) {
				t.Fatalf("Build error = %v, want %v", err, tc.target)
			}
		})
	}
}

func TestEmptyImage(t *testing.T) {
	img := mustBuild(t)
	if img.NumLayers() != 0 {
		t.Fatalf("NumLayers() = %d, want 0", img.NumLayers())
	}
	if _, err := img.ReadFile(0, "/x"); !errors.Is(err, rootfs.ErrLayerOutOfRange) {
		t.Fatalf("read from empty image: err = %v, want ErrLayerOutOfRange", err)
	}
}

func TestBuildCopiesInputContent(t *testing.T) {
	content := []byte("original")
	img := mustBuild(t, rootfs.Layer{Entries: []rootfs.Entry{
		rootfs.File("/f", content),
	}})

	content[0] = 'X' // mutate caller's buffer after Build
	if got := readString(t, img, 0, "/f"); got != "original" {
		t.Fatalf("stored content = %q, want %q", got, "original")
	}

	fc, err := img.ReadFile(0, "/f")
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	fc.Data[0] = 'Y' // mutate the returned buffer
	again, err := img.ReadFile(0, "/f")
	if err != nil {
		t.Fatalf("ReadFile again: %v", err)
	}
	if string(again.Data) != "original" {
		t.Fatalf("image mutated through returned slice: %q", again.Data)
	}
}

func TestEarlierLayersAreIndependent(t *testing.T) {
	img := mustBuild(t, baseLayers()...)

	// Layer 1's opaque marker must not have mutated the layer 0 view in place.
	l0, err := img.ListDir(0, "/etc/app")
	if err != nil {
		t.Fatalf("ListDir layer 0: %v", err)
	}
	if got, want := names(l0), []string{"app.yaml", "logging.conf"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("layer 0 children after layer 1 merge = %v, want %v", got, want)
	}
}
