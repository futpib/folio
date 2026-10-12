import java.awt.Window;
import java.nio.file.*;
import java.util.Set;
import javax.swing.*;

/** Exercise real Swing navigation, saving and verification without IBKR login. */
public final class ApiConfigurationTest {
    public static void main(String[] args) throws Exception {
        Path directory = Path.of(args[0]);
        Class<?> type = Class.forName("zond.ibgateway.ApiConfiguration");
        var constructor = type.getDeclaredConstructor(int.class); constructor.setAccessible(true);
        var tick = type.getDeclaredMethod("tick"); tick.setAccessible(true);
        for (String scenario : new String[]{"fresh", "custom-title", "already-configured", "missing", "ambiguous", "discarded"}) {
            GatewaySettingsFixture[] fixture = {null};
            SwingUtilities.invokeAndWait(() -> {
                fixture[0] = new GatewaySettingsFixture(directory);
                if (scenario.equals("custom-title")) fixture[0].frame.setTitle("My trading session");
                fixture[0].omitReadOnly = scenario.equals("missing");
                fixture[0].duplicatePort = scenario.equals("ambiguous");
                fixture[0].discardChanges = scenario.equals("discarded");
                if (scenario.equals("already-configured")) {
                    fixture[0].port = 14001; fixture[0].readOnly = true; fixture[0].localOnly = true;
                }
                fixture[0].frame.setVisible(true);
            });
            Object configuration = constructor.newInstance(14001);
            boolean[] ready = {false};
            long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(3);
            while (!ready[0] && System.nanoTime() < deadline) {
                SwingUtilities.invokeAndWait(() -> {
                    try { ready[0] = (boolean)tick.invoke(configuration); }
                    catch (Exception failure) { throw new RuntimeException(failure); }
                });
                Thread.sleep(20);
            }
            boolean expected = Set.of("fresh", "custom-title", "already-configured").contains(scenario);
            if (ready[0] != expected) throw new AssertionError(scenario + ": readiness " + ready[0]);
            if (expected && (fixture[0].port != 14001 || !fixture[0].readOnly || !fixture[0].localOnly
                || fixture[0].saves != 2)) throw new AssertionError("settings not saved and verified");
            if ((scenario.equals("missing") || scenario.equals("ambiguous")) && fixture[0].saves != 0)
                throw new AssertionError("unknown layout must not be saved");
            SwingUtilities.invokeAndWait(() -> { for (Window window : Window.getWindows()) window.dispose(); });
        }
        System.out.println("PASS: API defaults, existing settings, missing/ambiguous controls, save failure");
    }
}
