#!/bin/bash

# Cartella locale con i CSV
LOCAL_DIR="/Users/dmk6603/Documents/sec10k/1-flask-app/data"

# Destinazione sul server
REMOTE_USER="dima"
REMOTE_HOST="10.20.5.21"
REMOTE_DIR="/home/dima/sec10k/1-flask-app"

# Trasferimento
rsync -avh --progress "$LOCAL_DIR/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR"