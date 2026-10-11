import java.awt.*;
import java.nio.file.*;
import javax.swing.*;
import javax.swing.tree.*;

/** Test-only post-login settings UI; never connects to IBKR. */
public final class GatewaySettingsFixture {
    final JFrame frame = new JFrame("IB Gateway");
    int port = 7496;
    boolean readOnly, localOnly;
    boolean omitReadOnly, duplicatePort, discardChanges;
    int saves;

    GatewaySettingsFixture(Path directory) {
        JMenuBar bar = new JMenuBar();
        JMenu configure = new JMenu("Configure");
        JMenuItem settings = new JMenuItem("Settings");
        settings.addActionListener(event -> open(directory));
        configure.add(settings); bar.add(configure); frame.setJMenuBar(bar);
        frame.add(new JLabel("Authenticated test Gateway")); frame.pack();
    }

    private void open(Path directory) {
        JDialog dialog = new JDialog(frame, "Global Configuration", true);
        DefaultMutableTreeNode root = new DefaultMutableTreeNode("Configuration");
        DefaultMutableTreeNode api = new DefaultMutableTreeNode("API");
        DefaultMutableTreeNode settings = new DefaultMutableTreeNode("Settings");
        root.add(api); api.add(settings);
        JTree tree = new JTree(root);
        JPanel panel = new JPanel(); panel.setLayout(new BoxLayout(panel, BoxLayout.Y_AXIS));
        JCheckBox read = new JCheckBox("Read-Only API", readOnly);
        JCheckBox local = new JCheckBox("Allow connections from localhost only", localOnly);
        if (!omitReadOnly) panel.add(read);
        panel.add(local);
        JPanel socket = new JPanel(); socket.setName("Socket port");
        JTextField field = new JTextField(Integer.toString(port), 8);
        socket.add(new JLabel("Socket port")); socket.add(field); panel.add(socket);
        if (duplicatePort) socket.add(new JTextField("1234", 8));
        panel.setVisible(false);
        tree.addTreeSelectionListener(e -> {
            panel.setVisible(tree.getLastSelectedPathComponent() == settings); dialog.pack();
        });
        JButton ok = new JButton("OK"), cancel = new JButton("Cancel");
        ok.addActionListener(event -> {
            if (!discardChanges) { port = Integer.parseInt(field.getText()); readOnly = read.isSelected(); localOnly = local.isSelected(); }
            saves++;
            try {
                Files.writeString(directory.resolve("api-settings"), port + " " + readOnly + " " + localOnly);
            } catch (Exception failure) { throw new RuntimeException(failure); }
            dialog.dispose();
        });
        cancel.addActionListener(event -> dialog.dispose());
        JPanel buttons = new JPanel(); buttons.add(ok); buttons.add(cancel);
        dialog.add(tree, BorderLayout.WEST); dialog.add(panel, BorderLayout.CENTER);
        dialog.add(buttons, BorderLayout.SOUTH); dialog.pack(); dialog.setVisible(true);
    }
}
