package twopc

import "errors"

// ErrUnreachable is returned by a Link when the participant cannot be
// reached (or the reply was lost after the operation was persisted).
var ErrUnreachable = errors.New("twopc: participant unreachable")

// Link is a ParticipantRPC wrapper modelling an unreliable in-process
// channel to a participant. It can be Down, and it can drop the reply
// after the participant has already persisted and applied the operation
// (DropNextReply), which is exactly the "applied then lost the reply"
// failure mode.
type Link struct {
	P             *Participant
	Down          bool
	DropNextReply bool
}

func (l *Link) Prepare(txID string, delta int) (bool, error) {
	if l.Down {
		return false, ErrUnreachable
	}
	yes, err := l.P.Prepare(txID, delta)
	if l.DropNextReply {
		l.DropNextReply = false
		return false, ErrUnreachable
	}
	return yes, err
}

func (l *Link) Commit(txID string) error {
	if l.Down {
		return ErrUnreachable
	}
	err := l.P.Commit(txID)
	if l.DropNextReply {
		l.DropNextReply = false
		return ErrUnreachable
	}
	return err
}

func (l *Link) Abort(txID string) error {
	if l.Down {
		return ErrUnreachable
	}
	err := l.P.Abort(txID)
	if l.DropNextReply {
		l.DropNextReply = false
		return ErrUnreachable
	}
	return err
}
