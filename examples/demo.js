/**
 * Runnable demo: open-loop run where the first two dispatched requests are
 * deliberately slowed, then the mock backend recovers. The arrival schedule
 * for the second half is unchanged — only queue/service timings move.
 *
 * Run: node examples/demo.js
 */
import { VirtualClock, MockBackend, LoadGenerator } from '../src/index.js';

const clock = new VirtualClock();
let generator;

const backend = new MockBackend({
  serviceTime: 6, // normal service: 6 ms
  slowFirst: 2, // slow down exactly the first two dispatches
  slowDuration: 20, // those two take 20 ms instead
  clock,
  onComplete: (record) => generator.completionHandler(record),
});

generator = new LoadGenerator({
  clock,
  backend,
  concurrency: 2,
  queueCapacity: 2,
  arrivals: [0, 1, 2, 3, 4].map((t) => ({ time: t, id: `req-${t}` })),
});

generator.start();
clock.run();

console.log('completed (in completion order):');
for (const r of generator.completed) {
  console.log(
    `  ${r.id}: scheduled=${r.scheduledAt} dispatched=${r.dispatchedAt} ` +
      `completed=${r.completedAt} queueMs=${r.queueMs} serviceMs=${r.serviceMs}`,
  );
}
console.log('dropped:');
for (const d of generator.dropped) {
  console.log(`  ${d.id}: scheduled=${d.scheduledAt} reason=${d.reason} (inFlight=${d.inFlight}, queued=${d.queued})`);
}
