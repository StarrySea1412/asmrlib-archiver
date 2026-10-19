package com.asmrlib.archiver;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Environment;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.webkit.MimeTypeMap;

import java.io.File;
import java.io.FileNotFoundException;
import java.util.List;

/**
 * Minimal framework-only FileProvider for files the shell saved itself.
 *
 * The project deliberately carries no AndroidX
 * ({@code android.useAndroidX=false}), so {@code androidx.core.content
 * .FileProvider} is not available and cannot be added. Handing the
 * {@code file://} URI that DownloadManager reports through
 * {@code COLUMN_LOCAL_URI} to an {@code ACTION_VIEW} intent throws
 * {@code FileUriExposedException} on API 24+, so the download center resolves
 * a finished download to a {@code content://} URI served by this provider
 * instead, with a one-shot read grant.
 *
 * <p>Two locations are served, because the shell now saves in two places: a
 * direct DownloadManager task lands in the public Downloads directory, while
 * the in-app HLS downloader writes into this app's private files directory
 * ({@code getExternalFilesDir(null)/offline}) and must leave through the same
 * kind of URI.
 */
public class DownloadFileProvider extends ContentProvider {

    /** Must match android:authorities in AndroidManifest.xml. */
    public static final String AUTHORITY = "com.asmrlib.archiver.fileprovider";

    /** Path prefix routing a URI at the app-private offline directory. */
    private static final String OFFLINE = "offline";

    /**
     * Map a {@code file://} URI pointing at a file in the public Downloads
     * directory to the {@code content://} URI this provider serves.
     */
    public static Uri uriForFile(Uri fileUri) {
        if (fileUri == null) return null;
        String name = fileUri.getLastPathSegment();
        if (name == null || name.isEmpty()) return null;
        return new Uri.Builder().scheme("content").authority(AUTHORITY)
                .appendPath(name).build();
    }

    /**
     * Map an app-private saved file to the {@code content://} URI this provider
     * serves. The in-app HLS downloader writes concatenated MPEG-TS under
     * {@code getExternalFilesDir(null)/offline}, which the Downloads-shaped
     * overload above cannot express — and that output still must not be handed
     * to {@code ACTION_VIEW} as a {@code file://} URI.
     */
    public static Uri uriForFile(File file) {
        if (file == null) return null;
        String name = file.getName();
        if (name == null || name.isEmpty()) return null;
        return new Uri.Builder().scheme("content").authority(AUTHORITY)
                .appendPath(OFFLINE).appendPath(name).build();
    }

    /**
     * Two shapes are served: a bare name directly inside public Downloads, and
     * {@code offline/<name>} inside this app's private files directory.
     */
    private File resolve(Uri uri) {
        List<String> segments = uri == null ? null : uri.getPathSegments();
        if (segments == null || segments.isEmpty()) return null;
        if (segments.size() == 1) {
            String name = safeName(segments.get(0));
            if (name == null) return null;
            return new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS), name);
        }
        if (segments.size() == 2 && OFFLINE.equals(segments.get(0))) {
            String name = safeName(segments.get(1));
            if (name == null) return null;
            Context context = getContext();
            File base = context == null ? null : context.getExternalFilesDir(null);
            if (base == null) return null;
            return new File(new File(base, OFFLINE), name);
        }
        return null;
    }

    /** Only a bare file name is ever served — never a traversal or a subpath. */
    private static String safeName(String name) {
        if (name == null || name.isEmpty()) return null;
        if (name.contains("/") || name.contains("\\") || name.contains("..")) return null;
        return name;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        String name = uri == null ? null : uri.getLastPathSegment();
        if (name == null) return "application/octet-stream";
        int dot = name.lastIndexOf('.');
        if (dot < 0 || dot == name.length() - 1) return "application/octet-stream";
        String mime = MimeTypeMap.getSingleton()
                .getMimeTypeFromExtension(name.substring(dot + 1).toLowerCase());
        return mime == null ? "application/octet-stream" : mime;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        File file = resolve(uri);
        if (file == null || !file.isFile()) return null;
        String[] cols = projection != null ? projection
                : new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        MatrixCursor cursor = new MatrixCursor(cols, 1);
        Object[] row = new Object[cols.length];
        for (int i = 0; i < cols.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(cols[i])) row[i] = file.getName();
            else if (OpenableColumns.SIZE.equals(cols[i])) row[i] = file.length();
            else row[i] = null;
        }
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = resolve(uri);
        if (file == null || !file.isFile()) throw new FileNotFoundException(String.valueOf(uri));
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("DownloadFileProvider is read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("DownloadFileProvider is read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("DownloadFileProvider is read-only");
    }
}
