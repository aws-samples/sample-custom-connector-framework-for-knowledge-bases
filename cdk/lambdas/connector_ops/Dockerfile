FROM ghcr.io/astral-sh/uv:latest AS uv
FROM public.ecr.aws/docker/library/python:3.14-slim
COPY --from=uv /uv /usr/local/bin/uv
