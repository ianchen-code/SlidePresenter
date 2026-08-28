#!/bin/bash

set -e

echo "Starting PostgreSQL and MinIO..."
docker compose up -d postgres minio minio-init

echo ""
echo "Waiting for services to be ready..."
sleep 5

echo ""
echo "Services are running!"
echo ""
echo "PostgreSQL:"
echo "  Host: localhost:5432"
echo "  Database: slidepresenter"
echo "  User: slidepresenter"
echo "  Password: slidepresenter"
echo ""
echo "MinIO (S3-compatible):"
echo "  API: http://localhost:9000"
echo "  Console: http://localhost:9001"
echo "  User: minioadmin"
echo "  Password: minioadmin"
echo "  Bucket: slidepresenter-files"
echo ""
echo "Copy .env.example to .env and configure as needed:"
echo "  cp .env.example .env"
echo ""
echo "Then start the backend:"
echo "  cd backend && pip install -r requirements.txt"
echo "  uvicorn main:app --reload"
