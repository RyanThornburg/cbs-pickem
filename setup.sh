#!/bin/bash

set -e
ENV=${1:-local}

echo "Using environment: $ENV"

if [ "$ENV" != "local" ] && [ "$ENV" != "prod" ]; then
    echo "Unknown environment: $ENV"
    echo "Usage: ./setup.sh [local|prod]"
    exit 1
fi

if [ ! -f "config/.env.$ENV" ]; then
    if [ "$ENV" = "local" ]; then
        echo "Creating local environment file from template..."
        cp config/.env.example config/.env.local
        echo "Please edit config/.env.local with your D1 credentials"
        exit 1
    fi
    echo "Environment file config/.env.$ENV not found!"
    echo "Copy config/.env.example to config/.env.$ENV and configure it"
    exit 1
fi

echo "Creating tables..."
uv run python -m db.setup "$ENV"

echo "Database setup complete for $ENV environment!"
