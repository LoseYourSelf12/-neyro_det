#!/bin/bash
set -e
service ssh start
exec python src/__main__.py
