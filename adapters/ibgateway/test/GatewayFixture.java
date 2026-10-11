import java.io.*;
import java.net.*;
import java.nio.file.*;
import java.util.Arrays;
import javax.swing.*;
import javax.swing.event.*;

/** A real Swing window and local API handshake, with no IBKR network access. */
public class GatewayFixture {
    private static ServerSocket server;
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        Path signals = Path.of(args[1]);
        SwingUtilities.invokeAndWait(() -> {
            JFrame frame = new JFrame("IBKR Gateway");
            JPanel panel = new JPanel();
            JRadioButton fix = new JRadioButton("FIX CTCI", true), api = new JRadioButton("IB API");
            ButtonGroup protocol = new ButtonGroup(); protocol.add(fix); protocol.add(api);
            JRadioButton live = new JRadioButton("Live Trading", true), paper = new JRadioButton("Paper Trading");
            ButtonGroup trading = new ButtonGroup(); trading.add(live); trading.add(paper);
            JTextField username = new JTextField(12);
            JPasswordField password = new JPasswordField(12);
            JButton login = new JButton("Log In"); login.setEnabled(false);
            paper.addActionListener(e -> login.setText("Paper Log In"));
            DocumentListener fields = new DocumentListener() {
                public void insertUpdate(DocumentEvent e) { update(); }
                public void removeUpdate(DocumentEvent e) { update(); }
                public void changedUpdate(DocumentEvent e) { update(); }
                void update() { login.setEnabled(!username.getText().isEmpty() && password.getPassword().length > 0); }
            };
            username.getDocument().addDocumentListener(fields); password.getDocument().addDocumentListener(fields);
            login.addActionListener(e -> {
                if (!api.isSelected() || !paper.isSelected() || !username.getText().equals("test-user") || !Arrays.equals(password.getPassword(), "test-gateway-password".toCharArray())) throw new AssertionError("incorrect login controls");
                frame.setVisible(false);
                try {
                    Files.writeString(signals.resolve("submitted"), "yes");
                    server = new ServerSocket(port, 10, InetAddress.getLoopbackAddress());
                    new Thread(() -> {
                        while (!server.isClosed()) {
                            try (Socket connection = server.accept()) {
                                connection.setSoTimeout(2000);
                                DataInputStream input = new DataInputStream(connection.getInputStream());
                                if (!Arrays.equals(input.readNBytes(4), new byte[]{'A','P','I',0})) continue;
                                input.readNBytes(input.readInt());
                                DataOutputStream out = new DataOutputStream(connection.getOutputStream());
                                byte[] response = "178\00020261011 12:00:00 UTC\000".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
                                out.writeInt(response.length); out.write(response); out.flush();
                            } catch (IOException ignored) {}
                        }
                    }, "fixture-api").start();
                } catch (IOException failure) { throw new RuntimeException(failure); }
            });
            for (java.awt.Component c : new java.awt.Component[]{fix,api,live,paper,username,password,login}) panel.add(c);
            frame.add(panel); frame.pack(); frame.setVisible(true);
            new Timer(100, e -> {
                try {
                    if (Files.deleteIfExists(signals.resolve("reauth"))) {
                        if (server != null) server.close();
                        password.setText(""); frame.setVisible(true);
                    }
                    if (Files.deleteIfExists(signals.resolve("hide"))) frame.setVisible(false);
                } catch (IOException failure) { throw new RuntimeException(failure); }
            }).start();
        });
    }
}
