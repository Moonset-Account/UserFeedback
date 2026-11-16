"""Synthetic vulnerable Flask app used by secscan's own tests. Every finding is intentional."""
import os
import sqlite3
import subprocess

import requests
import yaml
from flask import Flask, request

app = Flask(__name__)


@app.route("/ping")
def ping():
    # command injection: request parameter reaches a shell
    return subprocess.check_output("ping -c 1 " + request.args["host"], shell=True)


@app.route("/user")
def user():
    # SQL injection: string formatting into a query
    conn = sqlite3.connect("app.db")
    return str(conn.execute("SELECT * FROM users WHERE id = '%s'" % request.args["id"]).fetchall())


@app.route("/proxy")
def proxy():
    # SSRF: caller-controlled URL fetched server-side
    return requests.get(request.args["url"]).text


@app.route("/load")
def load():
    # unsafe deserialization
    return str(yaml.load(request.data, Loader=yaml.Loader))


@app.route("/calc")
def calc():
    # eval on user input
    return str(eval(request.args["expr"]))


@app.route("/file")
def file():
    # path traversal
    with open(os.path.join("/srv/uploads", request.args["name"])) as f:
        return f.read()
