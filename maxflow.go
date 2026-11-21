package capacityreview

import "math"

// dinic is a small Dinic max-flow implementation over int64 capacities.
type flowEdge struct {
	to      int
	rev     int
	cap     int64
	initial int64
}

type dinic struct {
	adj   [][]flowEdge
	level []int
	iter  []int
}

func newDinic(n int) *dinic {
	return &dinic{
		adj:   make([][]flowEdge, n),
		level: make([]int, n),
		iter:  make([]int, n),
	}
}

func (d *dinic) addEdge(u, v int, cap int64) {
	if cap < 0 {
		panic("capacityreview: negative edge capacity")
	}
	fwd := flowEdge{to: v, rev: len(d.adj[v]), cap: cap, initial: cap}
	rev := flowEdge{to: u, rev: len(d.adj[u]), cap: 0, initial: 0}
	d.adj[u] = append(d.adj[u], fwd)
	d.adj[v] = append(d.adj[v], rev)
}

func (d *dinic) bfs(s, t int) bool {
	for i := range d.level {
		d.level[i] = -1
	}
	d.level[s] = 0
	queue := make([]int, 0, len(d.adj))
	queue = append(queue, s)
	for head := 0; head < len(queue); head++ {
		u := queue[head]
		for _, e := range d.adj[u] {
			if e.cap > 0 && d.level[e.to] < 0 {
				d.level[e.to] = d.level[u] + 1
				queue = append(queue, e.to)
			}
		}
	}
	return d.level[t] >= 0
}

func (d *dinic) dfs(u, t int, pushed int64) int64 {
	if u == t {
		return pushed
	}
	for ; d.iter[u] < len(d.adj[u]); d.iter[u]++ {
		idx := d.iter[u]
		e := &d.adj[u][idx]
		if e.cap <= 0 || d.level[e.to] != d.level[u]+1 {
			continue
		}
		got := pushed
		if e.cap < got {
			got = e.cap
		}
		got = d.dfs(e.to, t, got)
		if got <= 0 {
			continue
		}
		e.cap -= got
		d.adj[e.to][e.rev].cap += got
		return got
	}
	return 0
}

func (d *dinic) maxFlow(s, t int) int64 {
	var total int64
	for d.bfs(s, t) {
		for i := range d.iter {
			d.iter[i] = 0
		}
		for {
			pushed := d.dfs(s, t, math.MaxInt64)
			if pushed == 0 {
				break
			}
			total += pushed
		}
	}
	return total
}

// sourceSide returns the set of nodes reachable from s in the residual graph
// after maxFlow. It is the source side of a minimum s-t cut.
func (d *dinic) sourceSide(s int) []bool {
	seen := make([]bool, len(d.adj))
	seen[s] = true
	queue := []int{s}
	for head := 0; head < len(queue); head++ {
		u := queue[head]
		for _, e := range d.adj[u] {
			if e.cap > 0 && !seen[e.to] {
				seen[e.to] = true
				queue = append(queue, e.to)
			}
		}
	}
	return seen
}
