import java.awt.Window;
import java.lang.reflect.*;
import javax.swing.SwingUtilities;
import ibgateway.*;

/** Test-only session bootstrap for the installed Gateway 10.51, without remote login. */
final class GatewaySession {
    private final aw frame;
    private jsetting.ApiSettings settings;

    GatewaySession(Window window, Runnable ready) throws Exception {
        ax login = (ax)window;
        var credentials = new twslaunch.jclient.login.x("test-user",
            twslaunch.jutils.az.a("test-gateway-password".toCharArray()));
        login.a().ap().a(credentials); login.a().ao().a(credentials);
        twslaunch.jsetting.J.b(login.a().ap().b().a(), login.a().ap().b().b());
        frame = new aw(login);
        GatewayAdapter.a(frame);
        Method initialize = aw.class.getDeclaredMethod("j");
        initialize.setAccessible(true); initialize.invoke(frame);
        // Gateway's own offline initializer builds its real models and settings UI.
        Method mock = aw.class.getDeclaredMethod("a", Runnable.class);
        mock.setAccessible(true);
        mock.invoke(frame, (Runnable)() -> SwingUtilities.invokeLater(() -> {
            try {
                Method getter = GatewayAdapter.class.getDeclaredMethod("f");
                getter.setAccessible(true);
                jclient.gh control = (jclient.gh)getter.invoke(null);
                settings = control.R().P();
                field("m_port").set(settings, 14001);
                field("m_readOnlyApi").set(settings, false);
                field("m_allowOnlyLocalhost").set(settings, false);
                frame.b(control);
                // A user can customize the real Gateway title.
                frame.setTitle("My trading session");
                frame.setVisible(true);
                ready.run();
            } catch (Exception failure) { throw new RuntimeException(failure); }
        }));
    }

    void show(boolean visible) { frame.setVisible(visible); }

    String settings() throws Exception {
        if (settings == null) return "initializing";
        return field("m_port").get(settings) + " " + field("m_readOnlyApi").get(settings)
            + " " + field("m_allowOnlyLocalhost").get(settings);
    }

    private static Field field(String name) throws Exception {
        Field field = jsetting.ApiSettings.class.getDeclaredField(name);
        field.setAccessible(true); return field;
    }
}
