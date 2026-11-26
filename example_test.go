package sharedscan_test

import (
	"fmt"

	ss "sharedscan"
)

func exampleScalarRows(values ...ss.Datum) []ss.InnerRow {
	rows := make([]ss.InnerRow, 0, len(values))
	for _, value := range values {
		rows = append(rows, ss.InnerRow{Values: ss.Row{value}})
	}
	return rows
}

// Example prints the required boundary matrix: NULL left values, empty inner
// sets, and inner sets in which ordinary values and NULLs coexist.
func Example() {
	cases := []struct {
		label string
		left  ss.Row
		rule  ss.Rule
		inner []ss.InnerRow
	}{
		{
			label: "left=NULL; inner=empty; rule=SQL",
			left:  ss.Row{ss.NullDatum()},
			rule:  ss.SQLRule,
		},
		{
			label: "left=1; inner=empty; rule=SQL",
			left:  ss.Row{ss.Int(1)},
			rule:  ss.SQLRule,
		},
		{
			label: "left=NULL; inner=(1,2); rule=SQL",
			left:  ss.Row{ss.NullDatum()},
			rule:  ss.SQLRule,
			inner: exampleScalarRows(ss.Int(1), ss.Int(2)),
		},
		{
			label: "left=NULL; inner=(1,NULL); rule=SQL",
			left:  ss.Row{ss.NullDatum()},
			rule:  ss.SQLRule,
			inner: exampleScalarRows(ss.Int(1), ss.NullDatum()),
		},
		{
			label: "left=1; inner=(NULL,2); rule=SQL",
			left:  ss.Row{ss.Int(1)},
			rule:  ss.SQLRule,
			inner: exampleScalarRows(ss.NullDatum(), ss.Int(2)),
		},
		{
			label: "left=1; inner=(NULL,1); rule=SQL",
			left:  ss.Row{ss.Int(1)},
			rule:  ss.SQLRule,
			inner: exampleScalarRows(ss.NullDatum(), ss.Int(1)),
		},
		{
			label: "left=3; inner=(NULL,1); rule=SQL",
			left:  ss.Row{ss.Int(3)},
			rule:  ss.SQLRule,
			inner: exampleScalarRows(ss.NullDatum(), ss.Int(1)),
		},
		{
			label: "left=NULL; inner=(NULL); rule=NULL_AWARE",
			left:  ss.Row{ss.NullDatum()},
			rule:  ss.NullAwareRule,
			inner: exampleScalarRows(ss.NullDatum()),
		},
	}

	for _, tc := range cases {
		reqs := []ss.Request{
			{ID: "in", Expr: ss.ExprIn, Left: tc.left, Rule: tc.rule},
			{ID: "not-in", Expr: ss.ExprNotIn, Left: tc.left, Rule: tc.rule},
			{ID: "exists", Expr: ss.ExprExists, Rule: tc.rule},
		}
		op, err := ss.NewOperator(reqs)
		if err != nil {
			panic(err)
		}
		if err := op.Consume(tc.inner...); err != nil {
			panic(err)
		}
		results, err := op.Finish()
		if err != nil {
			panic(err)
		}
		fmt.Printf("%s: IN=%s NOT_IN=%s EXISTS=%s\n",
			tc.label, results[0].Value, results[1].Value, results[2].Value)
	}

	// Output:
	// left=NULL; inner=empty; rule=SQL: IN=FALSE NOT_IN=TRUE EXISTS=FALSE
	// left=1; inner=empty; rule=SQL: IN=FALSE NOT_IN=TRUE EXISTS=FALSE
	// left=NULL; inner=(1,2); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
	// left=NULL; inner=(1,NULL); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
	// left=1; inner=(NULL,2); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
	// left=1; inner=(NULL,1); rule=SQL: IN=TRUE NOT_IN=FALSE EXISTS=TRUE
	// left=3; inner=(NULL,1); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
	// left=NULL; inner=(NULL); rule=NULL_AWARE: IN=TRUE NOT_IN=FALSE EXISTS=TRUE
}
