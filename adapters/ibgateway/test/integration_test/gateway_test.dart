import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:dartssh2/dartssh2.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:zondd_control/control.dart';
import 'package:zondd_control/main.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  final fixture =
      jsonDecode(const String.fromEnvironment('ZONDD_FIXTURE'))
          as Map<String, dynamic>;
  final gateway = fixture['gateway'] as Map<String, dynamic>;
  for (final iroh in [false, true]) {
    testWidgets(
      'installed Gateway login and HTTP recovery over ${iroh ? 'Iroh' : 'SSH'}',
      (tester) async {
        tester.testTextInput.register();
        final directory = '${gateway['directory']}/${iroh ? 'iroh' : 'ssh'}';
        final profile = Profile(
          name: 'Gateway test server',
          host: fixture[iroh ? 'endpoint' : 'host'],
          iroh: iroh,
          port: fixture['port'],
          user: fixture['user'],
          privateKey: fixture['key'],
          executable: fixture['executable'],
          runtime: fixture['runtime'],
          config: fixture['config'],
        );
        final api = await SshControl.connect(
          profile,
          (_, fingerprint) async => fingerprint == fixture['fingerprint'],
        );
        final original = await api.request({'method': 'read_config'});
        final caller = SSHClient(
          await SSHSocket.connect(fixture['host'], fixture['port']),
          username: fixture['user'],
          identities: SSHKeyPair.fromPem(fixture['callerKey']),
          onVerifyHostKey: (_, fingerprint) =>
              utf8.decode(fingerprint) == fixture['fingerprint'],
        );

        Future<Map<String, dynamic>> control(String action) async {
          final session = await caller.execute('');
          final events = StreamIterator(
            session.stdout
                .cast<List<int>>()
                .transform(utf8.decoder)
                .transform(const LineSplitter()),
          );
          try {
            session.stdin.add(
              Uint8List.fromList(
                utf8.encode(
                  '${jsonEncode({
                    'version': 1,
                    'id': 1,
                    'body': {
                      'method': 'run',
                      'command': {
                        'program': '/usr/bin/python3',
                        'args': [gateway['script'], 'client', directory, action],
                      },
                    },
                  })}\n',
                ),
              ),
            );
            expect(
              await events.moveNext().timeout(const Duration(seconds: 20)),
              true,
            );
            final result = jsonDecode(events.current)['body'];
            expect(result['type'], 'completed', reason: jsonEncode(result));
            expect(result['code'], 0, reason: jsonEncode(result));
            return jsonDecode(utf8.decode(base64Decode(result['stdout'])))
                as Map<String, dynamic>;
          } finally {
            await events.cancel();
            session.close();
          }
        }

        Future<void> waitFor(Finder finder) async {
          for (var i = 0; i < 900 && finder.evaluate().isEmpty; i++) {
            await tester.pump(const Duration(milliseconds: 100));
          }
          expect(finder, findsOneWidget);
          await tester.ensureVisible(finder);
          await tester.pump(const Duration(milliseconds: 200));
        }

        Future<void> state(String key, bool expected) async {
          for (var i = 0; i < 40; i++) {
            final value = await control('state');
            expect(value['patch-failed'], false);
            if (value[key] == expected) return;
            await tester.pump(const Duration(milliseconds: 200));
          }
          fail('Gateway did not reach $key=$expected');
        }

        Future<void> disconnected() async {
          for (var i = 0; i < 40; i++) {
            final value = await control('status');
            if (value['code'] == 0 &&
                jsonDecode(value['stdout'])['tws_connected'] == false) {
              expect((await control('accounts'))['code'], isNot(0));
              return;
            }
            await tester.pump(const Duration(milliseconds: 200));
          }
          fail('HTTP did not observe the disconnected Gateway');
        }

        try {
          final rules = ['state', 'status', 'accounts', 'accept-mfa', 'reauth']
              .map(
                (action) =>
                    '\n[[principals.exec.rules]]\nprogram = "/usr/bin/python3"\nargs = ${jsonEncode([gateway['script'], 'client', directory, action])}\ndecision = "allow"\n',
              )
              .join();
          final content =
              '${(original['content'] as String).replaceFirst('command_timeout_secs = 2', 'command_timeout_secs = 120').replaceFirst('max_output_bytes = 1024', 'max_output_bytes = 8192')}$rules\n[credential_adapters.gateway]\nlabel = "Configured IBKR paper Gateway"\nprogram = "/usr/bin/python3"\nargs = ${jsonEncode([gateway['script'], 'adapter', directory, gateway['repo']])}\ncwd = "/"\n';
          final saved = await api.request({
            'method': 'save_config',
            'revision': original['revision'],
            'content': content,
          });
          await api.request({
            'method': 'reload_config',
            'revision': saved['revision'],
          });
          await tester.pumpWidget(
            MaterialApp(
              home: Dashboard(api: api, name: profile.name),
            ),
          );
          await waitFor(find.text('Application input requested'));
          expect(
            find.text('Gateway test server · Configured IBKR paper Gateway'),
            findsOneWidget,
          );
          await state('patched', true);
          await disconnected();

          await waitFor(find.text('Reject'));
          await tester.tap(find.text('Reject'));
          await tester.pump(const Duration(seconds: 1));
          expect(
            (await api.request({'method': 'pending'}))['interactions'],
            isEmpty,
          );
          await state('submitted', false);
          expect(
            jsonEncode(await api.request({'method': 'actions'})),
            contains('rejected'),
          );
          await api.request({'method': 'retry_adapter', 'adapter': 'gateway'});

          for (var attempt = 0; attempt < 2; attempt++) {
            await waitFor(find.text('Respond'));
            final challenge =
                ((await api.request({'method': 'pending'}))['interactions']
                        as List)
                    .single;
            await tester.tap(find.text('Respond'));
            await waitFor(
              find.byKey(const ValueKey('interaction-field-username')),
            );
            await tester.enterText(
              find.byKey(const ValueKey('interaction-field-username')),
              'test-user',
            );
            await tester.enterText(
              find.byKey(const ValueKey('interaction-field-password')),
              'test-gateway-password',
            );
            await tester.tap(find.text('Send response'));
            await state('submitted', true);
            await state('authenticated', false);
            await disconnected();
            await control('accept-mfa');
            await waitFor(find.text('Application status: ready'));
            final actions =
                (await api.request({'method': 'actions'}))['actions'] as List;
            expect(
              actions.where(
                (event) =>
                    event['action'] == 'interaction_result' &&
                    event['detail']['challenge_id'] ==
                        challenge['challenge_id'] &&
                    event['detail']['outcome'] == 'succeeded',
              ),
              hasLength(1),
            );
            Map<String, dynamic>? accounts;
            for (var i = 0; i < 40; i++) {
              accounts = await control('accounts');
              if (accounts['code'] == 0) break;
              await tester.pump(const Duration(milliseconds: 200));
            }
            expect(accounts!['code'], 0);
            expect(jsonDecode(accounts['stdout']), [
              {'account': 'DU424242'},
            ]);
            if (attempt == 0) {
              await control('reauth');
              await state('authenticated', false);
              await disconnected();
            }
          }
          final audit = jsonEncode(
            await api.request({'method': 'actions', 'limit': 1000}),
          );
          expect(audit, isNot(contains('test-gateway-password')));
          expect(audit, isNot(contains('test-user')));
        } finally {
          await tester.pumpWidget(const SizedBox.shrink());
          caller.close();
          final latest = await api.request({'method': 'read_config'});
          final saved = await api.request({
            'method': 'save_config',
            'revision': latest['revision'],
            'content': original['content'],
          });
          await api.request({
            'method': 'reload_config',
            'revision': saved['revision'],
          });
          await api.close();
        }
      },
      timeout: const Timeout(Duration(minutes: 6)),
    );
  }
}
