package zond.ibgateway;

import java.awt.*;
import java.util.ArrayList;
import java.util.List;
import javax.swing.*;
import javax.swing.tree.TreeModel;
import javax.swing.tree.TreePath;

/** Apply API defaults through Gateway's own settings dialog, on the EDT. */
final class ApiConfiguration {
    private final int port;
    private JFrame gateway;
    private JDialog dialog;
    private boolean opening, closing, saved, verified;

    ApiConfiguration(int port) { this.port = port; }

    void reset() {
        gateway = null; dialog = null;
        opening = false; closing = false; saved = false; verified = false;
    }

    boolean tick() {
        if (gateway != null && !gateway.isShowing()) reset();
        if (verified && !closing) return true;
        if (closing) {
            if (dialog.isShowing()) return false;
            closing = false; opening = false; dialog = null;
            if (verified) return true;
        }
        if (dialog == null) {
            for (Window window : Window.getWindows()) {
                if (window instanceof JDialog d && d.isShowing() && ownedBy(d, gateway)
                    && button(d, "OK") != null && button(d, "Cancel") != null
                    && children(d).stream().anyMatch(c -> c instanceof JTree)) {
                    dialog = d; break;
                }
            }
        }
        if (dialog == null) {
            if (!opening) open();
            return false;
        }
        JTree tree = children(dialog).stream().filter(c -> c instanceof JTree)
            .map(c -> (JTree)c).findFirst().orElseThrow();
        TreeModel model = tree.getModel();
        TreePath api = path(model, new TreePath(model.getRoot()), "API");
        if (api == null) return false;
        TreePath settings = path(model, api, "Settings");
        TreePath target = settings == null ? api : settings;
        if (!target.equals(tree.getSelectionPath())) {
            tree.setSelectionPath(target); tree.scrollPathToVisible(target); return false;
        }
        JCheckBox readOnly = checkbox(dialog, "Read-Only API");
        JCheckBox localOnly = checkbox(dialog, "Allow connections from localhost only");
        JTextField socketPort = portField(dialog);
        AbstractButton ok = button(dialog, "OK");
        // Unknown or ambiguous controls never count as successfully configured.
        if (readOnly == null || socketPort == null || ok == null || !ok.isEnabled()) return false;
        if (saved) {
            if (!readOnly.isSelected() || !Integer.toString(port).equals(socketPort.getText())
                || (localOnly != null && !localOnly.isSelected())) return false;
            verified = true;
        } else {
            if (!readOnly.isEnabled() && !readOnly.isSelected()) return false;
            if (!socketPort.isEditable() || !socketPort.isEnabled()) return false;
            if (localOnly != null && !localOnly.isEnabled() && !localOnly.isSelected()) return false;
            readOnly.setSelected(true);
            if (localOnly != null) localOnly.setSelected(true);
            socketPort.setText(Integer.toString(port));
            saved = true;
        }
        closing = true;
        // A modal dialog may run a nested event loop; do not block the scanner.
        SwingUtilities.invokeLater(ok::doClick);
        return false;
    }

    private void open() {
        for (Window window : Window.getWindows()) {
            if (!(window instanceof JFrame frame) || !frame.isShowing()
                || frame.getJMenuBar() == null) continue;
            JMenuBar bar = frame.getJMenuBar();
            for (int i = 0; i < bar.getMenuCount(); i++) {
                JMenu menu = bar.getMenu(i);
                if (menu == null || !"Configure".equals(menu.getText())) continue;
                for (int j = 0; j < menu.getItemCount(); j++) {
                    JMenuItem item = menu.getItem(j);
                    if (item != null && "Settings".equals(item.getText()) && item.isEnabled()) {
                        gateway = frame; opening = true;
                        SwingUtilities.invokeLater(item::doClick); return;
                    }
                }
            }
        }
    }

    private static boolean ownedBy(Window child, Window owner) {
        if (owner == null) return false;
        for (Window current = child.getOwner(); current != null; current = current.getOwner()) {
            if (current == owner) return true;
        }
        return false;
    }

    private static TreePath path(TreeModel model, TreePath parent, String name) {
        Object node = parent.getLastPathComponent();
        if (name.equals(node.toString())) return parent;
        for (int i = 0; i < model.getChildCount(node); i++) {
            TreePath found = path(model, parent.pathByAddingChild(model.getChild(node, i)), name);
            if (found != null) return found;
        }
        return null;
    }

    private static List<Component> children(Container root) {
        List<Component> result = new ArrayList<>();
        for (Component c : root.getComponents()) {
            if (!c.isVisible()) continue;
            result.add(c);
            if (c instanceof Container nested) result.addAll(children(nested));
        }
        return result;
    }

    private static AbstractButton button(Container root, String text) {
        var found = children(root).stream().filter(c -> c instanceof AbstractButton b && text.equals(b.getText()))
            .map(c -> (AbstractButton)c).toList();
        return found.size() == 1 ? found.get(0) : null;
    }

    private static JCheckBox checkbox(Container root, String text) {
        AbstractButton found = button(root, text);
        return found instanceof JCheckBox box ? box : null;
    }

    private static JTextField portField(Container root) {
        List<JTextField> fields = new ArrayList<>();
        for (Component c : children(root)) {
            // Gateway names the enclosing component "Socket port".
            if (!"Socket port".equalsIgnoreCase(c.getName())) continue;
            if (c instanceof JTextField field) fields.add(field);
            else if (c instanceof Container container) {
                fields.addAll(children(container).stream().filter(f -> f instanceof JTextField)
                    .map(f -> (JTextField)f).toList());
            }
        }
        return fields.size() == 1 ? fields.get(0) : null;
    }
}
