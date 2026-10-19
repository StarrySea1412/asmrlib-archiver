package com.asmrlib.archiver;

import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMuxer;

import java.io.File;
import java.io.FileDescriptor;
import java.nio.ByteBuffer;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Remux a saved MPEG-TS artifact into an MP4 container.
 *
 * <p>A container change, not a re-encode: {@link MediaExtractor} demuxes the
 * TS (the platform ships MPEG-TS extractors — they are what
 * {@code MediaPlayer} and {@code MediaMetadataRetriever} already use to play
 * and sniff these files), hands out compressed access units, and
 * {@link MediaMuxer} writes them into a bare MP4. No codec runs; a 950 MB
 * file costs one pass of IO, not a transcode, and the picture quality is
 * bit-identical.
 *
 * <p>Why the copy is worth it: the TS the in-app HLS saver writes is what
 * nothing else on a bare phone opens. Browsers hide it behind a
 * {@code video/mp2t} intent that does not resolve, and gallery/微信/剪辑
 * apps all want MP4. The saved file stays in place; the MP4 is a second
 * artifact the user can share or open anywhere.
 *
 * <p>Buffer sizing: the largest {@code KEY_MAX_INPUT_SIZE} any accepted
 * track declares, with a floor — TS packets are small but the demuxer may
 * assemble whole PES frames, and H.264 1080p frames reach hundreds of KB.
 * Flags map {@code SAMPLE_FLAG_SYNC} to {@code BUFFER_FLAG_KEY_FRAME}; the
 * demuxer only ever reports sync on keyframes, which is the only sync
 * concept MP4 cares about either.
 */
public final class TsToMp4 {

    private static final int MIN_BUFFER_BYTES = 1 << 20; // 1 MiB

    /** Bytes-per-progress-report: a callback per access unit would mean
     *  one main-thread hop per frame for nothing the eye can read. */
    private static final long PROGRESS_STEP_BYTES = 4 * 1024 * 1024L;

    /** Writes progress upstream while the copy runs. All from the worker
     *  thread; the callee owns its own hops. */
    public interface Progress {
        void onProgress(long bytesWritten, long totalBytes);
    }

    /** The outcome, named rather than implied: callers must distinguish
     *  "wrote an MP4" from "could not", and want the destination either way
     *  so a failure can clean its own partial file. */
    public static final class Result {
        public final boolean ok;
        public final String destination;
        public final long bytesWritten;
        /** The failure reason, in user-facing Chinese; null when ok. */
        public final String error;

        private Result(boolean ok, String destination, long bytes, String error) {
            this.ok = ok;
            this.destination = destination;
            this.bytesWritten = bytes;
            this.error = error;
        }
    }

    private TsToMp4() {
    }

    /** Remux {@code source} to {@code destination} on the calling thread.
     *  A partial destination is deleted on failure; the source is never
     *  touched. Blocking (a gigabyte takes seconds of IO) — call off the
     *  main thread. */
    public static Result convert(String source, String destination) {
        return convert(source, destination, null, null);
    }

    /** Remux to an already-open descriptor (a MediaStore row), reporting
     *  progress every few megabytes. The caller owns the row lifecycle —
     *  a failed copy here leaves the row PENDING for the caller to delete,
     *  because this class never touches a content URI it does not own.
     *  {@code destination} is carried through for diagnostics only. */
    public static Result convert(String source, String destination,
            FileDescriptor out, Progress progress) {
        MediaExtractor extractor = new MediaExtractor();
        MediaMuxer muxer = null;
        long written = 0;
        try {
            extractor.setDataSource(source);
            int trackCount = extractor.getTrackCount();
            Map<Integer, Integer> trackMap = new LinkedHashMap<>();
            int bufferSize = MIN_BUFFER_BYTES;
            for (int i = 0; i < trackCount; i++) {
                MediaFormat format = extractor.getTrackFormat(i);
                String mime = format.getString(MediaFormat.KEY_MIME);
                if (mime == null || mime.isEmpty()) continue;
                boolean media = mime.startsWith("video/") || mime.startsWith("audio/");
                if (!media) continue;
                trackMap.put(i, -1);
                if (format.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                    bufferSize = Math.max(bufferSize,
                            format.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE));
                }
            }
            if (trackMap.isEmpty()) {
                return new Result(false, destination, 0, "文件里没有可封装的音视频轨道");
            }
            muxer = out == null
                    ? new MediaMuxer(destination, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
                    : new MediaMuxer(out, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            for (Map.Entry<Integer, Integer> entry : trackMap.entrySet()) {
                entry.setValue(muxer.addTrack(extractor.getTrackFormat(entry.getKey())));
            }
            muxer.start();

            final long total = new File(source).length();
            long nextTick = 0;
            ByteBuffer buffer = ByteBuffer.allocateDirect(bufferSize);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            for (Map.Entry<Integer, Integer> entry : trackMap.entrySet()) {
                extractor.selectTrack(entry.getKey());
                while (true) {
                    buffer.clear();
                    int size = extractor.readSampleData(buffer, 0);
                    if (size < 0) break;
                    info.offset = 0;
                    info.size = size;
                    info.presentationTimeUs = extractor.getSampleTime();
                    info.flags = extractor.getSampleFlags()
                            == MediaExtractor.SAMPLE_FLAG_SYNC
                            ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0;
                    muxer.writeSampleData(entry.getValue(), buffer, info);
                    written += size;
                    extractor.advance();
                    // The row's bar and the notification both read this.
                    if (progress != null && written >= nextTick) {
                        nextTick = written + PROGRESS_STEP_BYTES;
                        progress.onProgress(written, total);
                    }
                }
            }
            // stop() finalises the index — skipping it leaves a playable-but
            // headerless file that some players refuse.
            muxer.stop();
            if (progress != null) progress.onProgress(written, total);
            return new Result(true, destination, written, null);
        } catch (Exception e) {
            // Half an MP4 is worse than none: remove the partial output so a
            // failed export never leaves a corrupt file the user could open.
            try {
                File partial = new File(destination);
                if (partial.exists() && partial.length() > 0) partial.delete();
            } catch (Exception ignored) {
                // Nothing to undo — the delete is best-effort hygiene.
            }
            return new Result(false, destination, written,
                    e.getMessage() == null ? e.toString() : e.getMessage());
        } finally {
            try {
                extractor.release();
            } catch (Exception ignored) {
            }
            if (muxer != null) {
                try {
                    muxer.release();
                } catch (Exception ignored) {
                }
            }
        }
    }
}
