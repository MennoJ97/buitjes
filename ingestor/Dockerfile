FROM python:3.12-slim

WORKDIR /app

# h5py and pyproj ship manylinux wheels, so no build toolchain is needed.
COPY ingestor/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ingestor/ ./

# Run as a fixed, non-root UID. The number is load-bearing and must match the
# one in backend/Dockerfile: /data is shared with the server, and write_atomic
# publishes through tempfile.mkstemp, which creates 0600 files. The reader can
# therefore only read them by being the *same user* - a shared group would not
# be enough, and neither would a different UID with the same name.
#
# Creating /data here also fixes the ownership Docker copies onto a *fresh*
# named volume. A volume that already exists keeps the ownership it has, so an
# existing deployment needs a one-off chown before this image can write to it:
#
#   docker compose down
#   docker run --rm -v buitjes_frames:/data alpine chown -R 10001:10001 /data
#   docker compose up -d
RUN groupadd --system --gid 10001 buitjes \
    && useradd --system --uid 10001 --gid 10001 --no-create-home buitjes \
    && mkdir -p /data \
    && chown buitjes:buitjes /data
USER buitjes

# /app belongs to root and is not writable now, which is the point - but it does
# mean the interpreter cannot cache bytecode there and would retry on every
# import. Say so once rather than letting it fail quietly each time.
ENV PYTHONDONTWRITEBYTECODE=1

ENV FRAME_DIR=/data
CMD ["python", "-u", "-m", "ingestor.main"]
