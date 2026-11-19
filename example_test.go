package authstack_test

import (
	"fmt"

	"example.com/authstack"
)

// ExampleStack_Run shows the canonical composition: an institution sub-stack
// that may satisfy the layer on its own, with a local password as fallback.
func ExampleStack_Run() {
	local := authstack.Func{
		ProviderName: "local-password",
		Fn: func(_ authstack.Request, _ authstack.PromptAsker) error {
			return nil
		},
	}
	institution := authstack.NewStack("institution",
		authstack.ProviderItem(authstack.Required, authstack.Func{
			ProviderName: "inst-sso",
			Fn: func(_ authstack.Request, _ authstack.PromptAsker) error {
				return nil
			},
		}),
	)

	admin := authstack.NewStack("admin",
		authstack.StackItem(authstack.Sufficient, institution),
		authstack.ProviderItem(authstack.Required, local),
	)

	res := admin.Run(authstack.Request{Username: "alice"}, nil)

	fmt.Println("passed:", res.Passed)
	fmt.Println("local ran:", res.Trace.ItemTraceFor("local-password").Outcome != authstack.NotRun)
	fmt.Println("prompts:", res.PromptTexts())
	// Output:
	// passed: true
	// local ran: false
	// prompts: []
}
