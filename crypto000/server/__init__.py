from flask import Flask, send_from_directory, jsonify, abort, Response
import os


DBASE = os.path.dirname(os.path.realpath(__file__))
MAX_LOG_LEN = 1500


def server(host, port, log_q, verbose=False):
    if not verbose:
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
    app = Flask(__name__)
    logs = {'1': []}

    @app.route('/js/<path:text>')
    def javascript(text):
        if not text.endswith('.js'):
            return abort(Response('Not a JS file'))
        js_dir = os.path.join(DBASE, 'js')
        for fn in os.listdir(js_dir):
            if fn == text:
                return send_from_directory(js_dir, text)

    @app.route("/api/log")
    def loggy():
        log = logs['1']
        while not log_q.empty():
            log.append(log_q.get())
        if len(log) > MAX_LOG_LEN:
            log = log[:MAX_LOG_LEN]
        logs['1'] = log
        return jsonify(list(reversed([str(x) for x in log])))

    @app.route('/')
    def index():
        return '<body></body><script src="js/index.js"></script>'

    # if not verbose:
    # import sys
    # cli = sys.modules['flask.cli']
    # cli.show_server_banner = lambda *x: None
    # print(f'Running Flask on http://{host}:{port}')
    app.run(host, port)
