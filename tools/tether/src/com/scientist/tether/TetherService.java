package com.scientist.tether;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.VpnService;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.util.Log;

import java.io.DataInputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Reverse-tethering VPN: every IPv4 packet of the phone goes to the PC (scirelay) through
 * "adb reverse localabstract:gnirehtet tcp:31416", and the answers come back the same way.
 * Wire protocol (gnirehtet compatible): relay sends a 4-byte client id, then raw IPv4 packets both ways.
 *
 * Difference with the stock gnirehtet client: when the phone has no Wi-Fi / mobile network, the VPN declares
 * itself as its own underlying network. Otherwise Android's legacy getActiveNetworkInfo() returns null and many
 * apps (Facebook Lite, ...) believe they are offline even though the tunnel works.
 */
public class TetherService extends VpnService {
    static final String ACTION_START = "com.scientist.tether.START";
    static final String ACTION_STOP = "com.scientist.tether.STOP";
    private static final String TAG = "SciTether";
    private static final String SOCKET_NAME = "gnirehtet";
    private static final int MTU = 16384;

    private final Object lock = new Object();
    private volatile boolean running;
    private volatile LocalSocket socket;
    private ParcelFileDescriptor tun;
    private Handler handler;
    private ConnectivityManager cm;
    private ConnectivityManager.NetworkCallback callback;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();   // null = restarted after being killed
        if (ACTION_STOP.equals(action)) {
            stopTether();
            return START_NOT_STICKY;
        }
        showNotification();
        if (!running) {
            startTether(intent == null ? null : intent.getStringExtra("dns"));
        }
        return START_STICKY;
    }

    private void startTether(String dns) {
        Builder b = new Builder()
                .setSession("SCIENTIST tether")
                .addAddress("10.0.0.2", 32)
                .addRoute("0.0.0.0", 0)
                .setMtu(MTU);
        for (String d : (dns == null || dns.trim().isEmpty() ? "8.8.8.8" : dns).split(",")) {
            if (!d.trim().isEmpty()) {
                b.addDnsServer(d.trim());
            }
        }
        b.setBlocking(true);
        if (Build.VERSION.SDK_INT >= 29) {
            b.setMetered(false);
        }
        ParcelFileDescriptor fd;
        try {
            fd = b.establish();
        } catch (Exception e) {
            Log.e(TAG, "establish failed", e);
            fd = null;
        }
        if (fd == null) {
            Log.e(TAG, "VPN not authorized");
            stopTether();
            return;
        }
        tun = fd;
        running = true;
        handler = new Handler(Looper.getMainLooper());
        cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        new Thread(this::downlinkLoop, "tether-down").start();
        new Thread(this::uplinkLoop, "tether-up").start();
        watchNetworks();
        Log.i(TAG, "VPN established");
    }

    // ---- "is there a real network?" handling -------------------------------------------------------------
    private void watchNetworks() {
        if (Build.VERSION.SDK_INT < 22) {
            return;
        }
        callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                handler.postDelayed(TetherService.this::fixUnderlyingNetworks, 300);
            }

            @Override
            public void onLost(Network network) {
                handler.postDelayed(TetherService.this::fixUnderlyingNetworks, 300);
            }
        };
        try {
            cm.registerNetworkCallback(new NetworkRequest.Builder().build(), callback);
        } catch (Exception e) {
            callback = null;
        }
        handler.postDelayed(this::fixUnderlyingNetworks, 500);
    }

    private void fixUnderlyingNetworks() {
        try {
            applyUnderlyingNetworks();
        } catch (Exception e) {
            Log.w(TAG, "network check failed", e);   // never let this take the tether down
        }
    }

    private void applyUnderlyingNetworks() {
        if (!running || Build.VERSION.SDK_INT < 22) {
            return;
        }
        Network vpn = null;
        boolean realNetwork = false;
        for (Network n : cm.getAllNetworks()) {
            NetworkCapabilities c = cm.getNetworkCapabilities(n);
            if (c == null) {
                continue;
            }
            if (c.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                vpn = n;
            } else if (c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                realNetwork = true;
            }
        }
        try {
            if (realNetwork || vpn == null) {
                setUnderlyingNetworks(null);
                Log.i(TAG, "underlying network: system default");
            } else {
                setUnderlyingNetworks(new Network[]{vpn});
                Log.i(TAG, "underlying network: self (no Wi-Fi / mobile network)");
            }
        } catch (Exception e) {
            Log.w(TAG, "setUnderlyingNetworks failed", e);
        }
    }

    // ---- packet pumps ------------------------------------------------------------------------------------
    /** PC → phone. Also owns the connection to the relay and reconnects after a tunnel reset. */
    private void downlinkLoop() {
        byte[] buf = new byte[1 << 18];
        while (running) {
            LocalSocket s = new LocalSocket();
            try {
                s.connect(new LocalSocketAddress(SOCKET_NAME, LocalSocketAddress.Namespace.ABSTRACT));
                InputStream in = s.getInputStream();
                int id = new DataInputStream(in).readInt();
                socket = s;
                Log.i(TAG, "connected to relay as #" + id);
                FileOutputStream out = new FileOutputStream(tun.getFileDescriptor());
                int len = 0;
                while (running) {
                    int r = in.read(buf, len, buf.length - len);
                    if (r < 0) {
                        throw new IOException("relay closed the tunnel");
                    }
                    len += r;
                    int pos = 0;
                    while (len - pos >= 20) {
                        int version = (buf[pos] & 0xff) >> 4;
                        int total = version == 4
                                ? ((buf[pos + 2] & 0xff) << 8) | (buf[pos + 3] & 0xff)
                                : 40 + (((buf[pos + 4] & 0xff) << 8) | (buf[pos + 5] & 0xff));
                        if (total < 20 || total > buf.length) {
                            throw new IOException("bad packet length " + total);
                        }
                        if (len - pos < total) {
                            break;
                        }
                        if (version == 4) {
                            out.write(buf, pos, total);
                        }
                        pos += total;
                    }
                    if (pos > 0) {
                        System.arraycopy(buf, pos, buf, 0, len - pos);
                        len -= pos;
                    }
                }
            } catch (IOException e) {
                if (running) {
                    Log.w(TAG, "relay disconnected: " + e.getMessage());
                }
            } finally {
                socket = null;
                try {
                    s.close();
                } catch (IOException ignored) {
                }
            }
            if (running) {
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException ignored) {
                }
            }
        }
    }

    /** Phone → PC. Packets produced while the relay is unreachable are dropped (TCP will retransmit). */
    private void uplinkLoop() {
        byte[] packet = new byte[MTU];
        FileInputStream in = new FileInputStream(tun.getFileDescriptor());
        while (running) {
            int n;
            try {
                n = in.read(packet);
            } catch (IOException e) {
                break;                                   // tun closed: stopping
            }
            LocalSocket s = socket;
            if (n <= 0 || s == null) {
                continue;
            }
            try {
                synchronized (lock) {
                    OutputStream out = s.getOutputStream();
                    out.write(packet, 0, n);
                }
            } catch (IOException e) {
                try {
                    s.close();                           // the downlink loop notices and reconnects
                } catch (IOException ignored) {
                }
            }
        }
    }

    // ---- lifecycle ---------------------------------------------------------------------------------------
    private void showNotification() {
        Notification.Builder nb;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            nm.createNotificationChannel(new NotificationChannel("tether", "Reverse tether",
                    NotificationManager.IMPORTANCE_LOW));
            nb = new Notification.Builder(this, "tether");
        } else {
            nb = new Notification.Builder(this);
        }
        Notification n = nb.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("SCIENTIST tether")
                .setContentText("Internet from the PC over USB")
                .setOngoing(true)
                .build();
        startForeground(1, n);
    }

    private void stopTether() {
        running = false;
        if (callback != null && cm != null) {
            try {
                cm.unregisterNetworkCallback(callback);
            } catch (Exception ignored) {
            }
            callback = null;
        }
        LocalSocket s = socket;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }
        if (tun != null) {
            try {
                tun.close();
            } catch (IOException ignored) {
            }
            tun = null;
        }
        stopForeground(true);
        stopSelf();
        Log.i(TAG, "stopped");
    }

    @Override
    public void onRevoke() {
        stopTether();
    }

    @Override
    public void onDestroy() {
        if (running) {
            stopTether();
        }
        super.onDestroy();
    }
}
