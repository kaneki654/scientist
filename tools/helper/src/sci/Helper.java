package sci;

import android.app.WallpaperManager;
import android.content.Context;
import android.content.ContextWrapper;
import android.os.Looper;

import java.io.FileInputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

/**
 * Tiny on-device helper for SCIENTIST, run as the adb shell user through app_process
 * (the same technique scrcpy uses):
 *
 *   CLASSPATH=/data/local/tmp/scientist-helper.jar app_process / sci.Helper wallpaper <png> [both|system|lock]
 *
 * The shell user holds android.permission.SET_WALLPAPER, so no root and no taps are needed.
 */
public final class Helper {

    private static final String SHELL_PACKAGE = "com.android.shell";

    /** Context that identifies as the shell package, so app-op checks match uid 2000. */
    private static final class ShellContext extends ContextWrapper {
        ShellContext(Context base) {
            super(base);
        }

        @Override
        public String getPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public String getOpPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }
    }

    private static Context systemContext() throws Exception {
        if (Looper.getMainLooper() == null) {
            Looper.prepareMainLooper();
        }
        Class<?> atClass = Class.forName("android.app.ActivityThread");
        Constructor<?> ctor = atClass.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object thread = ctor.newInstance();
        Field current = atClass.getDeclaredField("sCurrentActivityThread");
        current.setAccessible(true);
        current.set(null, thread);
        Method getSystemContext = atClass.getDeclaredMethod("getSystemContext");
        return (Context) getSystemContext.invoke(thread);
    }

    private static int setWallpaper(String path, String target) throws Exception {
        int flags;
        if ("system".equals(target)) {
            flags = WallpaperManager.FLAG_SYSTEM;
        } else if ("lock".equals(target)) {
            flags = WallpaperManager.FLAG_LOCK;
        } else {
            flags = WallpaperManager.FLAG_SYSTEM | WallpaperManager.FLAG_LOCK;
        }
        Context ctx = new ShellContext(systemContext());
        WallpaperManager wm = WallpaperManager.getInstance(ctx);
        // The instance is bound to the system context ("android"); rebind it to the shell identity.
        try {
            Field f = WallpaperManager.class.getDeclaredField("mContext");
            f.setAccessible(true);
            f.set(wm, ctx);
        } catch (Throwable ignored) {
            // field layout differs on this Android version: keep the default
        }
        try (FileInputStream in = new FileInputStream(path)) {
            return wm.setStream(in, null, true, flags);
        }
    }

    /** What apps see: the legacy active NetworkInfo (used by many apps) and the modern active network. */
    @SuppressWarnings("deprecation")
    private static void netinfo() throws Exception {
        Context ctx = new ShellContext(systemContext());
        android.net.ConnectivityManager cm =
                (android.net.ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
        android.net.NetworkInfo ni = cm.getActiveNetworkInfo();
        System.out.println("activeNetworkInfo=" + (ni == null ? "null" : ni.getTypeName() + "/" + ni.getState()
                + " connected=" + ni.isConnected()));
        android.net.Network n = cm.getActiveNetwork();
        System.out.println("activeNetwork=" + n);
        if (n != null) {
            System.out.println("capabilities=" + cm.getNetworkCapabilities(n));
        }
    }

    public static void main(String[] args) {
        try {
            if (args.length >= 1 && "netinfo".equals(args[0])) {
                netinfo();
                System.exit(0);
            }
            if (args.length >= 2 && "wallpaper".equals(args[0])) {
                int id = setWallpaper(args[1], args.length >= 3 ? args[2] : "both");
                System.out.println("OK wallpaper id=" + id);
                System.exit(id > 0 ? 0 : 3);
            }
            System.out.println("usage: wallpaper <png> [both|system|lock]");
            System.exit(2);
        } catch (Throwable t) {
            Throwable root = t;
            while (root.getCause() != null) {
                root = root.getCause();
            }
            System.out.println("ERROR " + root);
            System.exit(1);
        }
    }
}
