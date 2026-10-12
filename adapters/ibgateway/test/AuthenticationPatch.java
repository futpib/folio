import java.awt.*;
import java.nio.file.*;
import java.util.*;
import javax.swing.*;

/** Test only: replace the installed Gateway's login action, never its controls. */
public final class AuthenticationPatch {
    private static Path directory;
    private static Window login;
    private static GatewaySession settings;
    private static String savedSettings;
    private static final Set<AbstractButton> patched = Collections.newSetFromMap(new IdentityHashMap<>());

    public static void premain(String options) {
        directory = Path.of(options);
        Thread.setDefaultUncaughtExceptionHandler((thread, failure) -> failed(failure));
        SwingUtilities.invokeLater(() -> new javax.swing.Timer(50, event -> scan()).start());
    }

    private static java.util.List<Component> children(Container root) {
        var result = new ArrayList<Component>();
        for (Component child : root.getComponents()) {
            if (!child.isVisible()) continue;
            result.add(child);
            if (child instanceof Container container) result.addAll(children(container));
        }
        return result;
    }

    private static void mark(String name) throws Exception {
        Files.writeString(directory.resolve(name), "yes");
    }

    private static void failed(Throwable failure) {
        try {
            var trace = new java.io.StringWriter();
            failure.printStackTrace(new java.io.PrintWriter(trace));
            Files.writeString(directory.resolve("patch-failed"), trace.toString());
        } catch (Exception ignored) {}
    }

    private static void scan() {
        try {
            if (Files.deleteIfExists(directory.resolve("reauth")) && login != null) {
                Files.deleteIfExists(directory.resolve("authenticated"));
                Files.deleteIfExists(directory.resolve("submitted"));
                if (settings != null) settings.show(false);
                login.setVisible(true);
            }
            if (Files.deleteIfExists(directory.resolve("hide")) && login != null) login.setVisible(false);
            if (Files.deleteIfExists(directory.resolve("accept-mfa"))) {
                if (!Files.exists(directory.resolve("submitted"))) throw new IllegalStateException("no login submitted");
                if (settings == null) settings = new GatewaySession(login, () -> {
                    try { mark("authenticated"); } catch (Exception failure) { failed(failure); }
                });
                else { settings.show(true); mark("authenticated"); }
            }
            if (settings != null) {
                String current = settings.settings();
                if (!current.equals(savedSettings)) {
                    Files.writeString(directory.resolve("api-settings"), current);
                    savedSettings = current;
                }
            }
            for (Window window : Window.getWindows()) {
                if (!window.isShowing()) continue;
                var controls = children(window);
                for (Component control : controls) {
                    if (!(control instanceof AbstractButton button) ||
                        !Set.of("Log In", "Paper Log In").contains(button.getText()) || !patched.add(button)) continue;
                    // The original listeners must never see even dummy credentials.
                    for (var listener : button.getActionListeners()) button.removeActionListener(listener);
                    button.addActionListener(event -> {
                        try {
                            var current = children(window);
                            var passwords = current.stream().filter(c -> c instanceof JPasswordField && c.isEnabled()).map(c -> (JPasswordField)c).toList();
                            var names = current.stream().filter(c -> c instanceof JTextField && !(c instanceof JPasswordField) && c.isEnabled() && ((JTextField)c).isEditable()).map(c -> (JTextField)c).toList();
                            if (passwords.size() != 1 || names.size() != 1) throw new IllegalStateException("unexpected controls");
                            char[] password = passwords.get(0).getPassword();
                            boolean valid = Arrays.equals(password, "test-gateway-password".toCharArray()) && names.get(0).getText().equals("test-user");
                            Arrays.fill(password, '\0');
                            if (!valid) throw new IllegalStateException("unexpected test credentials");
                            names.get(0).setText(""); passwords.get(0).setText("");
                            login = window;
                            window.setVisible(false);
                            mark("submitted");
                        } catch (Exception failure) {
                            failed(failure);
                        }
                    });
                    mark("patched");
                }
            }
        } catch (Exception failure) {
            failed(failure);
        }
    }
}
