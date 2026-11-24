package graphwalk_test

import (
	"context"
	"errors"
	"fmt"

	"graphwalk"
)

func ExampleCursor_Next() {
	reader := graphwalk.NewMemoryReader()
	page := graphwalk.PageInfo{
		ID: "outgoing", Version: "v1", From: "s",
		MinLabel: "a", MaxLabel: "a", MinCost: 0,
	}
	reader.Put(page, []graphwalk.Edge{
		{ID: 1, From: "s", To: "s", Label: "a", Cost: 0},
		{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
	})
	catalog := graphwalk.NewCatalog("catalog-v1", page)
	expr, err := graphwalk.ParseExpr("a*")
	if err != nil {
		panic(err)
	}
	cursor, err := graphwalk.NewCursor(graphwalk.Query{
		Source: "s", Target: "s", Expr: expr, MaxHops: 4,
	}, 2, catalog, reader.ReadPage)
	if err != nil {
		panic(err)
	}
	for {
		w, err := cursor.Next(context.Background())
		if errors.Is(err, graphwalk.ErrDone) {
			break
		}
		if err != nil {
			panic(err)
		}
		fmt.Printf("ids=%v cost=%d hops=%d\n", w.IDs, w.Cost, w.Hops)
	}
	// Output:
	// ids=[] cost=0 hops=0
	// ids=[1] cost=0 hops=1
}
