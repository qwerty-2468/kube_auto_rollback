FROM python:3.12-alpine
WORKDIR /app
COPY app.py .
# Always the healthy build: the test faults are env vars of the pod, set in
# rollback-platform (gitops/apps/<app>/base/release.yaml), not baked in here.
ARG APP_VERSION=v1
ARG COLOR=#2563eb
ENV APP_VERSION=$APP_VERSION COLOR=$COLOR
EXPOSE 8080
CMD ["python", "app.py"]
