// Package flowctl implements a deterministic shared-switch-buffer link
// flow-control model. Flows are identified by ingress port and priority,
// share one byte buffer, and use pause/resume directives backed by explicit
// in-flight reservations.
package flowctl
