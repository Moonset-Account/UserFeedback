package authstack

import (
	"errors"
	"fmt"
)

// ErrProviderUnavailable reports that a provider could not be consulted at all,
// for example because its backend is down. It is treated exactly like a failure
// of that item: the item's control flag applies, and the parent stack (if any)
// sees only an ordinary item failure.
var ErrProviderUnavailable = errors.New("authstack: provider unavailable")

// ErrPromptUnsupported reports that a provider needed an interactive prompt but
// the runner could not open an interaction channel. It is a failure of the item
// and is recorded without producing a prompt event.
var ErrPromptUnsupported = errors.New("authstack: provider requested interaction but no prompt handler is available")

// Request carries the credentials for a single stack invocation.
type Request struct {
	// Username identifies the account being authenticated.
	Username string
	// Secret is the credential material supplied by the end user.
	Secret string
}

// Prompt is an interactive question a provider wants to ask the end user.
type Prompt struct {
	// Provider is the provider name that requested the interaction, after any
	// intermediate sub-stack entry was flattened away.
	Provider string
	// Text is the question to show.
	Text string
	// Secret marks prompts whose answer must be masked on input.
	Secret bool
}

// Responder answers a Prompt. It is only ever called by the executor, never by
// a provider directly, so the caller keeps full control over the interaction.
type Responder interface {
	Respond(p Prompt) (string, error)
}

// Provider is one authentication method that may be placed inside a stack.
//
// Authenticate must return nil on success and a non-nil error on failure. It may
// request user interaction through PromptAsker; a provider that needs input must
// ask for it through that channel so the executor can record the prompt.
type Provider interface {
	// Name is the provider identifier used in reports, prompts and errors.
	Name() string
	// Authenticate performs the check. Returning ErrProviderUnavailable (or a
	// wrapped form of it) is equivalent to returning any other failure.
	Authenticate(req Request, ask PromptAsker) error
}

// PromptAsker is handed to providers so they can request end-user interaction.
type PromptAsker interface {
	// Ask shows a prompt and returns the answer. An error means the interaction
	// was refused, cancelled or unsupported; the provider should treat that as
	// its own failure.
	Ask(p Prompt) (string, error)
}

// Func adapts a bare function to the Provider interface.
type Func struct {
	ProviderName string
	Fn           func(req Request, ask PromptAsker) error
}

// Name implements Provider.
func (f Func) Name() string { return f.ProviderName }

// Authenticate implements Provider.
func (f Func) Authenticate(req Request, ask PromptAsker) error {
	if f.Fn == nil {
		return fmt.Errorf("authstack: provider %q has no function", f.ProviderName)
	}
	return f.Fn(req, ask)
}
