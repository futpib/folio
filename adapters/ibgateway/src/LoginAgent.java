package zond.ibgateway;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.awt.Component;
import java.awt.Container;
import java.awt.Window;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import javax.swing.*;

/** Translate the Gateway login UI into application-neutral zond forms. */
public final class LoginAgent {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final BlockingQueue<JsonNode> replies = new LinkedBlockingQueue<>(32);
    private static PrintStream protocol;
    private static String mode;
    private static int apiPort;
    private static ApiConfiguration apiConfiguration;
    private static volatile boolean apiConfigured;
    private static volatile boolean busy;
    private static volatile Window suppressed;
    private static volatile Login login;
    private static volatile String status = "starting";
    private static final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "zond-gateway-login"); t.setDaemon(true); return t;
    });

    public static void premain(String options) throws Exception {
        String[] parts = options.split(":");
        if (parts.length != 2 || !Set.of("paper", "live").contains(parts[0])) throw new IllegalArgumentException("expected mode:port");
        mode = parts[0]; apiPort = Integer.parseInt(parts[1]);
        if (apiPort < 1 || apiPort > 65535) throw new IllegalArgumentException("invalid API port");
        apiConfiguration = new ApiConfiguration(apiPort);
        InputStream incoming = System.in;
        protocol = System.out;
        System.setIn(InputStream.nullInputStream());
        System.setOut(new PrintStream(OutputStream.nullOutputStream()));
        System.setErr(new PrintStream(OutputStream.nullOutputStream()));
        Thread reader = new Thread(() -> {
            try (BufferedReader lines = new BufferedReader(new InputStreamReader(incoming, StandardCharsets.UTF_8))) {
                while (true) {
                    // Broker replies contain at most 16 bounded answer fields.
                    StringBuilder line = new StringBuilder();
                    int c;
                    while ((c = lines.read()) != '\n') {
                        if (c == -1 || line.length() >= 1024 * 1024) throw new IOException("protocol closed");
                        line.append((char)c);
                    }
                    JsonNode message = JSON.readTree(line.toString());
                    if (message.path("type").asText().equals("retry")) { suppressed = null; }
                    else { replies.put(message); }
                }
            } catch (Exception ignored) { Runtime.getRuntime().halt(1); }
        }, "zond-gateway-protocol");
        reader.setDaemon(true); reader.start();
        worker.submit(() -> {
            try {
                JsonNode ready = replies.take();
                if (!ready.path("type").asText().equals("ready") || ready.path("version").asInt() != 1) throw new IOException("unsupported broker");
                SwingUtilities.invokeLater(() -> new javax.swing.Timer(500, event -> scan()).start());
            } catch (Exception ignored) { Runtime.getRuntime().halt(1); }
        });
    }

    private record Login(Window window, JTextField username, JPasswordField password, AbstractButton submit) {}

    private static List<Component> components(Container root) {
        List<Component> result = new ArrayList<>();
        for (Component c : root.getComponents()) {
            if (!c.isVisible()) continue;
            result.add(c);
            if (c instanceof Container child) result.addAll(components(child));
        }
        return result;
    }

    private static Login findLogin() {
        for (Window window : Window.getWindows()) {
            if (!window.isShowing()) continue;
            String title = window instanceof JFrame f ? f.getTitle() : window instanceof JDialog d ? d.getTitle() : "";
            if (!Set.of("Login", "New Login", "IB Gateway", "IBKR Gateway").contains(title)) continue;
            List<Component> children = components(window);
            AbstractButton apiButton = children.stream().filter(c -> c instanceof AbstractButton b && "IB API".equals(b.getText())).map(c -> (AbstractButton)c).findFirst().orElse(null);
            if (apiButton != null && !apiButton.isSelected()) { apiButton.doClick(); return null; }
            AbstractButton modeButton = children.stream().filter(c -> c instanceof AbstractButton b && (mode.equals("paper") ? "Paper Trading" : "Live Trading").equals(b.getText())).map(c -> (AbstractButton)c).findFirst().orElse(null);
            if (modeButton != null && !modeButton.isSelected()) { modeButton.doClick(); return null; }
            List<JPasswordField> passwords = children.stream().filter(c -> c instanceof JPasswordField && c.isEnabled()).map(c -> (JPasswordField)c).toList();
            List<JTextField> names = children.stream().filter(c -> c instanceof JTextField && !(c instanceof JPasswordField) && c.isEnabled() && ((JTextField)c).isEditable()).map(c -> (JTextField)c).toList();
            if (passwords.size() != 1 || names.size() != 1) continue;
            String expected = mode.equals("paper") ? "Paper Log In" : "Log In";
            AbstractButton submit = children.stream().filter(c -> c instanceof AbstractButton b && expected.equals(b.getText())).map(c -> (AbstractButton)c).findFirst().orElse(null);
            if (submit != null) return new Login(window, names.get(0), passwords.get(0), submit);
        }
        return null;
    }

    private static void scan() {
        login = findLogin();
        if (login != null) {
            apiConfiguration.reset(); apiConfigured = false;
        } else {
            try { apiConfigured = apiConfiguration.tick(); }
            catch (RuntimeException ignored) { apiConfigured = false; }
        }
        if (busy) return;
        Login found = login;
        if (found != null && found.window() != suppressed) {
            busy = true;
            worker.submit(() -> authenticate(found));
        } else if (found == null) {
            suppressed = null;
            busy = true;
            worker.submit(() -> {
                try { setStatus(apiReady() ? "ready" : "unavailable"); }
                catch (Exception ignored) { Runtime.getRuntime().halt(1); }
                finally { busy = false; }
            });
        }
    }

    private static synchronized void send(ObjectNode message) throws IOException {
        protocol.println(JSON.writeValueAsString(message));
        protocol.flush();
        if (protocol.checkError()) throw new IOException("broker closed");
    }

    private static JsonNode receive(String expected) throws Exception {
        while (true) {
            JsonNode message = replies.take();
            String type = message.path("type").asText();
            if (type.equals("error")) throw new IOException("broker rejected request");
            if (type.equals(expected)) return message;
        }
    }

    private static void setStatus(String next) throws Exception {
        if (next.equals(status)) return;
        send(JSON.createObjectNode().put("method", "status").put("state", next));
        receive("recorded"); status = next;
    }

    private static void result(String id, String outcome) throws Exception {
        send(JSON.createObjectNode().put("method", "result").put("challenge_id", id).put("outcome", outcome));
        receive("recorded");
    }

    private static void authenticate(Login target) {
        String id = null;
        boolean answered = false;
        try {
            ObjectNode request = JSON.createObjectNode().put("method", "challenge")
                .put("prompt", "Sign in to IB Gateway (" + mode + "). Approve IBKR Mobile authentication when prompted.");
            request.putArray("fields")
                .add(JSON.createObjectNode().put("id", "username").put("label", "IBKR username").put("kind", "text"))
                .add(JSON.createObjectNode().put("id", "password").put("label", "IBKR password").put("kind", "secret"));
            request.putObject("context").put("application", "IB Gateway").put("mode", mode)
                .put("api_port", apiPort).put("read_only_api", true);
            send(request);
            id = receive("opened").path("challenge_id").asText();
            JsonNode answer;
            boolean withdrawing = false;
            while (true) {
                answer = replies.poll(500, TimeUnit.MILLISECONDS);
                if (answer != null && answer.path("type").asText().equals("answered")) break;
                if (!withdrawing && (login == null || login.window() != target.window())) {
                    send(JSON.createObjectNode().put("method", "withdraw").put("challenge_id", id));
                    withdrawing = true;
                }
            }
            JsonNode values = answer.path("values");
            if (!values.isObject()) { suppressed = target.window(); return; }
            answered = true;
            if (withdrawing || login == null || login.window() != target.window()) {
                result(id, "cancelled"); return;
            }
            // Re-check the exact controls on the EDT before delivering any secrets.
            boolean[] submitted = {false};
            SwingUtilities.invokeAndWait(() -> {
                Login current = findLogin();
                if (current == null || current.window() != target.window() || current.password() != target.password()) return;
                current.username().setText(values.path("username").asText());
                current.password().setText(values.path("password").asText());
                if (current.submit().isEnabled()) { current.submit().doClick(); submitted[0] = true; }
            });
            ((ObjectNode)values).removeAll();
            if (!submitted[0]) { result(id, "failed"); setStatus("failed"); suppressed = target.window(); return; }
            setStatus("working");
            long deadline = System.nanoTime() + TimeUnit.MINUTES.toNanos(5);
            while (System.nanoTime() < deadline) {
                if (login == null && apiReady()) { result(id, "succeeded"); setStatus("ready"); return; }
                Thread.sleep(1000);
            }
            result(id, "failed"); setStatus("failed"); suppressed = target.window();
        } catch (Exception ignored) {
            // Neither application errors nor credential values go to logs or stdout.
            if (answered && id != null) {
                try { result(id, "failed"); } catch (Exception ignoredAgain) {}
            }
            try { setStatus("failed"); } catch (Exception ignoredAgain) {}
            suppressed = target.window();
        } finally {
            SwingUtilities.invokeLater(() -> target.password().setText(""));
            busy = false;
        }
    }

    private static boolean apiReady() {
        if (!apiConfigured) return false;
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress(InetAddress.getLoopbackAddress(), apiPort), 500);
            socket.setSoTimeout(500);
            DataOutputStream out = new DataOutputStream(socket.getOutputStream());
            byte[] version = "v100..178".getBytes(StandardCharsets.US_ASCII);
            out.write(new byte[]{'A','P','I',0}); out.writeInt(version.length); out.write(version); out.flush();
            DataInputStream in = new DataInputStream(socket.getInputStream());
            int size = in.readInt();
            if (size < 4 || size > 4096) return false;
            String[] response = new String(in.readNBytes(size), StandardCharsets.US_ASCII).split("\u0000");
            return response.length >= 2 && Integer.parseInt(response[0]) >= 100;
        } catch (Exception ignored) { return false; }
    }
}
