# Pinned to a point release so rebuilds don't silently change the base.
FROM python:3.12.13-slim

WORKDIR /app
COPY pyproject.toml /app/
# Install only the dependency set ([project].dependencies); the apps are
# run as plain scripts, never imported as an installed package, so an
# (editable) install of the package itself is unnecessary. uv reads
# pyproject.toml directly when passed via -r. Keeping this layer free
# of the scripts preserves the dependency cache across code edits.
RUN pip install --no-cache-dir uv \
 && uv pip install --system --no-cache -r pyproject.toml

COPY app.py load_test.py /app/

ENTRYPOINT ["python", "app.py"]
