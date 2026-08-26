import java.util.ArrayList;
import java.util.BitSet;
import java.util.List;
import java.util.Locale;
import java.util.Random;

/**
 * Synthetic Fleet-style managed-object workload.
 * Metrics are application-provided proxies, not ART GC internals.
 */
public final class ObjectWorkload {
    private static final class Obj {
        final byte[] payload;
        Obj(int bytes) { this.payload = new byte[bytes]; }
        void touch(int value) { payload[value % payload.length]++; }
    }

    public static void main(String[] args) throws Exception {
        int objectBytes = Integer.parseInt(arg(args, "--object-bytes", "512"));
        int footprintMb = Integer.parseInt(arg(args, "--footprint-mb", "180"));
        int backgroundSeconds = Integer.parseInt(arg(args, "--background-seconds", "30"));
        int holdSeconds = Integer.parseInt(arg(args, "--hold-seconds", "600"));
        double hotFraction = Double.parseDouble(arg(args, "--hot-fraction", "0.10"));
        if (objectBytes <= 0 || footprintMb <= 0 || backgroundSeconds < 0 || holdSeconds < 0) {
            throw new IllegalArgumentException("object/footprint must be positive and durations non-negative");
        }
        long payloadBytes = footprintMb * 1024L * 1024L;
        int count = Math.toIntExact(payloadBytes / objectBytes);
        List<Obj> objects = new ArrayList<>(count);
        for (int i = 0; i < count; i++) objects.add(new Obj(objectBytes));

        BitSet accessedBefore = new BitSet(count);
        Random random = new Random(2024);
        int foregroundTouches = Math.max(1, count / 2);
        for (int i = 0; i < foregroundTouches; i++) {
            int index = random.nextInt(count);
            objects.get(index).touch(i);
            accessedBefore.set(index);
        }
        System.out.printf(Locale.ROOT, "{\"event\":\"ready\",\"pid\":%d,\"object_bytes\":%d,\"objects\":%d,\"payload_bytes\":%d}%n",
                ProcessHandle.current().pid(), objectBytes, count, payloadBytes);
        System.out.flush();
        Thread.sleep(backgroundSeconds * 1000L);

        BitSet hotAccessed = new BitSet(count);
        int hotTouches = Math.max(1, (int) (count * hotFraction));
        long start = System.nanoTime();
        for (int i = 0; i < hotTouches; i++) {
            int index = random.nextInt(count);
            objects.get(index).touch(i);
            hotAccessed.set(index);
        }
        long end = System.nanoTime();
        BitSet reaccessed = (BitSet) hotAccessed.clone();
        reaccessed.and(accessedBefore);
        double ratio = hotAccessed.cardinality() == 0 ? 0.0 :
                (double) reaccessed.cardinality() / hotAccessed.cardinality();
        long heapUsed = Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory();
        System.out.printf(Locale.ROOT, "{\"event\":\"hot_launch\",\"latency_ms\":%.3f,\"distinct_accessed\":%d,\"distinct_reaccessed\":%d,\"object_reaccess_ratio\":%.6f,\"java_heap_used_bytes\":%d}%n",
                (end - start) / 1e6, hotAccessed.cardinality(), reaccessed.cardinality(), ratio, heapUsed);
        System.out.flush();
        Thread.sleep(holdSeconds * 1000L);
        System.out.printf(Locale.ROOT, "{\"event\":\"done\",\"pid\":%d,\"reason\":\"hold_timeout\"}%n",
                ProcessHandle.current().pid());
    }

    private static String arg(String[] args, String name, String defaultValue) {
        for (int i = 0; i + 1 < args.length; i++) if (args[i].equals(name)) return args[i + 1];
        return defaultValue;
    }
}
