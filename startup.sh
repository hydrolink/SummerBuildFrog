#!/bin/bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker auth_server:app
