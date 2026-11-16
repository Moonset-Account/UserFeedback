// Synthetic vulnerable Express app used by secscan's own tests. Every finding here is intentional.
// Do not copy any of this into a real service.
import { exec } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import express from 'express';
import mysql from 'mysql';

const app = express();
const db = mysql.createConnection({ host: 'localhost', user: 'app', password: process.env.DB_PASSWORD });

// command injection: request parameter reaches a shell
app.get('/ping', (req, res) => {
  exec('ping -c 1 ' + req.query.host, (err, out) => res.send(out));
});

// SQL injection: string concatenation into a query
app.get('/user', (req, res) => {
  db.query("SELECT * FROM users WHERE id = '" + req.query.id + "'", (err, rows) => res.json(rows));
});

// SSRF: caller-controlled URL fetched server-side
app.get('/proxy', async (req, res) => {
  const r = await fetch(req.query.url);
  res.send(await r.text());
});

// path traversal: request parameter joined into a filesystem path
app.get('/file/:name', (req, res) => {
  const p = path.join('/srv/uploads', req.params.name);
  res.send(fs.readFileSync(p));
});

// prototype pollution: attacker-controlled key written to an object
app.post('/prefs', express.json(), (req, res) => {
  const prefs = {};
  for (const [k, v] of Object.entries(req.body)) prefs[k] = v;
  res.json(prefs);
});

// eval on user input
app.get('/calc', (req, res) => {
  res.send(String(eval(req.query.expr)));
});

app.listen(3000);
