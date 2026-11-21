# rootfs — layered root filesystem query module

`rootfs` is a small Go package that an image builder can use to reconstruct a
container-style root filesystem from an ordered stack of layers and then query
**any layer prefix** by path. Built for Go 1.20.2 with the standard library only.

The motivating case: a base layer ships `etc/app/app.yaml` and
`etc/app/logging.conf`; a new layer marks `etc/app` **opaque** and writes a fresh
`etc/app/app.yaml`. After the merge the caller sees only the new config, yet can
still query layer 0 and read the original two configs.

## Public API (`package rootfs`)

### Input

```go
type Kind int
const (
    KindFile     Kind = iota // regular file, content in Entry.Content
    KindDir                  // directory
    KindWhiteout             // ordinary deletion marker
    KindOpaque               // opaque-directory marker
)

type Entry struct {
    Path    string
    Kind    Kind
    Content []byte // used only by KindFile; copied by Build
}

type Layer struct { Entries []Entry }
```

Helpers: `File(path, content)`, `Dir(path)`, `Whiteout(path)`, `Opaque(path)`.

`Build(layers []Layer) (*Image, error)` merges layers in order. Entry order inside
a layer is irrelevant: entries are canonicalized, validated and sorted before
merging. `Build` copies all content and never retains caller slices.

### Queries

```go
func (img *Image) NumLayers() int

func (img *Image) ReadFile(layer int, path string) (FileContent, error)
func (img *Image) Stat(layer int, path string)     (FileInfo, error)
func (img *Image) ListDir(layer int, path string)  ([]DirEntry, error)
```

`layer` is zero-based and selects the merged view of layers `0..layer`.
Results carry provenance:

| Type | Fields |
| --- | --- |
| `FileContent` | `Path` (canonical), `Data` (private copy), `Size`, `Layer` |
| `FileInfo` | `Path`, `IsDir`, `Size`, `Layer` |
| `DirEntry` | `Name`, `IsDir`, `Size`, `Layer` |

`ListDir` returns direct children sorted by name and works on `"/"`. The
synthetic root reports `Layer == -1` from `Stat`.

Errors (all wrapable with `errors.Is`): `ErrNotExist`, `ErrNotDir`,
`ErrIsDir`, `ErrLayerOutOfRange`, `ErrInvalidPath`, `ErrInvalidEntry`,
`ErrDuplicatePath`, `ErrConflict`.

## Merge semantics

Applied per layer against the inherited view (`view(0..n-1)`):

1. **Ordinary whiteout** at `P`: removes the inherited item at `P` *and its
   whole subtree*. If the inherited `P` is a directory, all descendants go too.
2. **Opaque marker** at `P`: hides only the children inherited at `P`. The
   directory `P` itself remains; if no inherited directory exists there, an
   empty directory is created at `P`.
3. **Content** placed by the same layer is then installed. Because removals are
   computed against the inherited view *before* the layer's own records are
   inserted, **anything the same layer adds survives its own whiteouts and
   opaque markers** — including a file re-added at a path the layer also whited
   out.
4. **Replacements truncate**: a file at `P` replaces an inherited directory and
   drops all of its descendants; a directory at `P` replaces an inherited file.
   `ListDir` on the resulting file returns `ErrNotDir`; reads through a file
   used as an ancestor return `ErrNotExist`.

Each layer view is an independent deep copy of the previous view, so querying
an earlier layer always shows the pre-merge state. Views are read-only after
`Build`, so concurrent queries are safe.

### Path handling

Entry and query paths may be absolute or relative. They are cleaned
(`//a/./b` → `/a/b`); `..` is rejected outright with `ErrInvalidPath` rather
than resolved. The root path itself may not be an entry (`ErrInvalidEntry`).
Two records canonicalizing to the same path are rejected (`ErrDuplicatePath` or
`ErrConflict`), as are any records beneath a path the same layer claims as a
regular file (content, whiteout, or opaque) — a malformed layer description
rather than a merge to interpret.

Accepted same-path combinations inside one layer: `File` + `Whiteout` (hide the
lower item, then replace it), and `Dir` + `Opaque` (a real layer commonly
carries both).

## Reproduce and verify

Requires Go 1.20.2. The module has no dependencies.

```sh
cd <this workspace>
go test ./...            # run the suite
go test -v ./...         # per-test output
go test -race ./...      # race detector
```

`go.mod` declares `module rootfs` / `go 1.20`. The only third-party input is the
Go toolchain; no network access is needed or performed.

## Verification scope (real tests, not canned output)

All tests are in `rootfs_test.go`, use real assertions on real return values,
and run against the local in-memory module only. None of the outputs in this
README are hand-authored; every scenario is asserted in code.

Normal scenarios
- `TestNormalScenarioOpaqueReplacesInheritedChildren` — the prompt's base +
  opaque-layer case: newer `app.yaml` visible from layer 1, inherited
  `logging.conf` gone, sibling `hostname` kept, layer 0 still shows both old
  configs.
- `TestLayerProvenanceThroughStack` — three layers overwriting one file; each
  view reads its own version and reports the correct origin layer; a file only
  in layer 0 reports layer 0 at the top view.
- `TestOpaqueDoesNotHideSameLayerChildren` — children added by the opaque layer
  survive.
- `TestWhiteoutThenRecreatedByLaterLayer` — a whiteout in layer 1, content
  reintroduced in layer 2.

Boundary scenarios
- `TestWhiteoutHidesSubtree` — whiteout of a directory hides it and every
  descendant, including nested ones.
- `TestFileReplacingDirectoryDropsDescendants` — file over a directory drops
  all old descendants; reads through the file ancestor return `ErrNotExist`.
- `TestDirectoryReplacingFile` — directory over a file.
- `TestOpaqueDirectoryCreatedOnlyByMarker` — opaque marker alone creates an
  empty directory.
- `TestOpaqueOverInheritedFileBecomesDirectory` — opaque marker over an
  inherited file yields an empty directory.
- `TestEmptyImage` — zero layers; every query is out of range.
- `TestBuildCopiesInputContent` — caller mutation of input and of returned
  bytes does not affect the image.
- `TestQueryErrors` — out-of-range layers (negative and past the end), missing
  paths, `NotDir`, `IsDir`, `..` escape, empty path.
- `TestPathsAreNormalized` — relative/redundant-separator paths resolve to one
  canonical path; colliding records are rejected.
- `TestBuildRejectsBadInput` — root entry, directory carrying content, unknown
  kind, path escape, duplicate file, file+dir at one path, file as ancestor,
  whiteout and opaque markers beneath a same-layer file, plus the accepted
  whiteout+file pairing.

Interleaving / order scenarios
- `TestSameLayerContentSurvivesItsOwnMarkers` — one layer whitens and rewrites
  the same file and marks its directory opaque, with shuffled entries; the new
  content wins and the earlier layer is untouched.
- `TestEntryOrderDoesNotMatter` — forward vs. reversed entry slices produce the
  identical visible tree.
- `TestWhiteoutOfParentWithSameLayerChild` — whiteout of a directory while the
  same layer adds a child under it: only the inherited subtree is hidden.
- `TestEarlierLayersAreIndependent` — layer 0's directory listing still has
  both original configs after the opaque merge.
- `TestDirectoryProvenance` — explicit directory records and implicitly created
  ancestors report the layer that produced them.

Not run in this environment: nothing in the scope below was skipped. The suite
runs fully offline. `go vet ./...` was attempted but the sandbox's permission
policy required approval for that command; `gofmt -l .` was run and reported no
files needing formatting.
