# Issue #14: the gateway, in a booted NixOS VM, against a fake Bridge.
#
# Three nodes. `bridge` runs the fake Hue Bridge (nix/fake-hue.nix) with a
# certificate the gateway verifies for real. `gateway` runs the NixOS module
# in static-bridge mode, handed its Application Key the way a real deployment
# is — a Credentials File loaded by systemd `LoadCredential`, referenced by
# runtime path, never in the store or an `ExecStart` argument. `paired` runs
# the same module with no static bridge: it pairs on first start, writes a
# `registry.json`, and is the node the restart-persistence step exercises.
#
# The Application Key is generated inside the VM at test time and written to
# both nodes by the test script, so it exists nowhere in the Nix store. That
# is what lets the last step — grep the journal, find no key — mean something.
{ pkgs, self }:

let
  system = pkgs.stdenv.hostPlatform.system;
  hue-grpc = self.packages.${system}.hue-grpc;
  fake-hue = self.packages.${system}.fake-hue;

  # Uppercase in configuration, lowercase in the certificate's common name:
  # exactly the split a real Bridge presents, and what the gateway's
  # case-insensitive identity check exists for.
  bridgeId = "ECB5FAFFFE334703";

  # An unprivileged port, so the fake needs no capability to bind it; the
  # gateway is told the port as part of the address.
  bridgePort = 8443;
  bridgeAddress = "bridge:${toString bridgePort}";

  # Minted once at build time. The gateway trusts this CA (bridge.caFile) and
  # the fake serves this leaf; the private key never leaves the `bridge` node.
  bridgeCerts = pkgs.runCommand "fake-hue-bridge-certs" { } ''
    ${fake-hue}/bin/fake-hue mint-certs --bridge-id ${bridgeId} --out $out
  '';
  bridgeCa = "${bridgeCerts}/ca.pem";

  credentialsPath = "/run/hue-grpc-secret/credentials";
  # A plain /run path, not a unit RuntimeDirectory: the fake is stopped and
  # started again mid-test, and RuntimeDirectory cleanup would take the key
  # with it.
  keyPath = "/run/fake-hue-application-key";

  # Pair once, on the paired node's first start; a no-op once the registry
  # names the Bridge. Runs as the service's own user (systemd shares it with
  # ExecStartPre), so the registry.json it writes is the one ExecStart reads.
  pairOnce = pkgs.writeShellScript "hue-grpc-pair-once" ''
    set -eu
    if [ -f "$STATE_DIRECTORY/registry.json" ]; then
      echo "already paired"; exit 0
    fi
    for _ in $(seq 1 20); do
      if ${hue-grpc}/bin/hue-grpc-server pair \
        --bridge-address ${bridgeAddress} --bridge-id ${bridgeId} \
        --bridge-ca-file ${bridgeCa}; then
        exit 0
      fi
      sleep 2
    done
    echo "pairing never succeeded" >&2; exit 1
  '';

  # Shared by the gateway nodes: DEBUG is where the redacted-header line
  # lives, so it is also what the journal-grep step checks.
  debugArgs = [
    "--log-level"
    "DEBUG"
  ];
in
pkgs.testers.runNixOSTest {
  name = "hue-grpc-integration";

  nodes = {
    bridge =
      { ... }:
      {
        networking.firewall.allowedTCPPorts = [ bridgePort ];

        systemd.services.fake-hue = {
          description = "Fake Philips Hue Bridge";
          # Started by the test script, once the Application Key file exists.
          wantedBy = [ ];
          unitConfig.ConditionPathExists = keyPath;
          serviceConfig = {
            # Serving is the default; there is no `serve` subcommand.
            ExecStart = builtins.concatStringsSep " " [
              "${fake-hue}/bin/fake-hue"
              "--bridge-id ${bridgeId}"
              "--listen-address 0.0.0.0 --port ${toString bridgePort}"
              "--cert-dir ${bridgeCerts}"
              "--application-key-file ${keyPath}"
              "--log"
            ];
            Type = "exec";
            DynamicUser = true;
          };
        };
      };

    gateway =
      { lib, ... }:
      {
        imports = [ self.nixosModules.default ];
        environment.systemPackages = [ pkgs.grpcurl ];

        services.hue-grpc = {
          enable = true;
          package = hue-grpc;
          bridge = {
            address = bridgeAddress;
            id = bridgeId;
            credentialsFile = credentialsPath;
            caFile = bridgeCa;
          };
          extraArgs = debugArgs;
        };

        # The test script writes the Credentials File and starts the service;
        # without this the unit would fail-loop at boot with nothing to load.
        systemd.services.hue-grpc.wantedBy = lib.mkForce [ ];
      };

    paired =
      { lib, ... }:
      {
        imports = [ self.nixosModules.default ];
        environment.systemPackages = [ pkgs.grpcurl ];

        services.hue-grpc = {
          enable = true;
          package = hue-grpc;
          # No static bridge: this gateway pairs and reads registry.json.
          bridge.caFile = bridgeCa;
          extraArgs = debugArgs;
        };

        systemd.services.hue-grpc = {
          wantedBy = lib.mkForce [ ];
          # The registry.json this writes on first start is what ExecStart
          # reads; a later restart finds it already there and reuses it, so a
          # restart that still serves is one that reloaded persisted state.
          serviceConfig.ExecStartPre = [ "${pairOnce}" ];
        };
      };
  };

  testScript = ''
    import json

    start_all()

    GRPCURL = "grpcurl -plaintext -d '{}' 127.0.0.1:50051 "


    def subscriber_count(node):
        out = node.succeed(
            "journalctl -u hue-grpc.service | grep -c 'subscriber joined' || true"
        )
        return int(out.strip())


    def wait_for_subscriber(node, more_than):
        node.wait_until_succeeds(
            "test $(journalctl -u hue-grpc.service "
            f"| grep -c 'subscriber joined') -gt {more_than}",
            timeout=30,
        )


    def update_light(node, light, group, value):
        body = f'{{"lightId":"{light}","command":{{"{group}":{value}}}}}'
        return node.succeed(
            f"grpcurl -plaintext -d '{body}' 127.0.0.1:50051 "
            "hue.v1.LightingService/UpdateLight"
        )


    def poke_light(node, light):
        # Change the light once a second for a while, in the background: the
        # gateway only forwards events for changes made after its upstream
        # stream is up, and that can lag a fresh subscriber by a moment.
        body = f'{{"lightId":"{light}","command":{{"dimming":{{"brightness":55}}}}}}'
        node.succeed(
            "for _ in $(seq 30); do "
            f"grpcurl -plaintext -d '{body}' 127.0.0.1:50051 "
            "hue.v1.LightingService/UpdateLight >/dev/null 2>&1 || true; "
            "sleep 1; done & echo poking"
        )


    key_file = "${keyPath}"
    creds = "${credentialsPath}"

    # An Application Key that exists only in the running VMs.
    key = bridge.succeed("head -c 18 /dev/urandom | base64").strip()

    bridge.succeed(f"printf '%s' '{key}' > {key_file}")
    bridge.systemctl("start fake-hue.service")
    bridge.wait_for_unit("fake-hue.service")
    bridge.wait_for_open_port(${toString bridgePort}, timeout=60)

    # Step 3: the key reaches the gateway as a Credentials File, nothing more.
    gateway.succeed("install -d -m 0700 /run/hue-grpc-secret")
    gateway.succeed(f"install -m 0600 /dev/null {creds}")
    gateway.succeed(f"printf 'application-key=%s' '{key}' > {creds}")

    # Steps 4 and 5: the unit starts and systemd calls it healthy.
    gateway.systemctl("start hue-grpc.service")
    gateway.wait_for_unit("hue-grpc.service")
    gateway.wait_for_open_port(50051)
    gateway.succeed("systemctl is-active hue-grpc.service")

    # Step 6: the standard gRPC health endpoint.
    gateway.succeed(GRPCURL + "grpc.health.v1.Health/Check | grep -q SERVING")

    # Issue #15: record the hardening score against a unit that is up and
    # serving, and fail if it regresses out of the "OK" band the empirical
    # tightening pass reached.
    report = gateway.succeed("systemd-analyze security --no-pager hue-grpc.service")
    print(report)
    overall = [ln for ln in report.splitlines() if "Overall exposure level" in ln]
    assert overall, report
    exposure = float(overall[0].split(":")[1].split()[0])
    assert exposure < 4.0, overall[0]

    # Step 7a: one read.
    listed = json.loads(gateway.succeed(GRPCURL + "hue.v1.LightingService/ListLights"))
    assert any(
        light["metadata"]["name"] == "Desk" for light in listed["lights"]
    ), listed
    light_id = listed["lights"][0]["id"]

    # Step 7b: one mutation.
    mutation = update_light(gateway, light_id, "dimming", '{"brightness":42}')
    assert light_id in mutation, mutation

    # Step 7c: one event stream. Attach a subscriber, wait for the gateway to
    # confirm it joined, then change a light and see the change arrive.
    before = subscriber_count(gateway)
    gateway.succeed(
        "timeout 40 " + GRPCURL + "hue.v1.EventService/Subscribe "
        "> /tmp/events.json 2>/tmp/events.err & echo started"
    )
    wait_for_subscriber(gateway, before)
    poke_light(gateway, light_id)
    gateway.wait_until_succeeds("grep -q '\"change\"' /tmp/events.json", timeout=40)

    # Step 8: a paired gateway, restarted, still serves the same Bridge from
    # the registry.json it wrote on first start — the persisted state here.
    paired.systemctl("start hue-grpc.service")
    paired.wait_for_unit("hue-grpc.service")
    paired.wait_for_open_port(50051)
    registry = json.loads(paired.succeed("cat /var/lib/hue-grpc/registry.json"))
    assert registry["bridge"]["id"] == "${bridgeId}", registry
    assert registry["bridge"]["application_key"], registry
    minted_before = registry["bridge"]["application_key"]
    paired.wait_until_succeeds(
        GRPCURL + "hue.v1.LightingService/ListLights | grep -q Desk", timeout=20
    )

    paired.systemctl("restart hue-grpc.service")
    paired.wait_for_unit("hue-grpc.service")
    paired.wait_for_open_port(50051)
    reloaded = json.loads(paired.succeed("cat /var/lib/hue-grpc/registry.json"))
    assert reloaded["bridge"]["application_key"] == minted_before, reloaded
    paired.wait_until_succeeds(
        GRPCURL + "hue.v1.LightingService/ListLights | grep -q Desk", timeout=20
    )
    # Out of the way of the bridge-interruption step, which restarts the fake.
    paired.systemctl("stop hue-grpc.service")

    # Step 9: the bridge drops off the network. The gRPC stream does not end;
    # the gateway reconnects and announces a Gap with CAUSE_RECONNECTED, then
    # keeps serving.
    before = subscriber_count(gateway)
    gateway.succeed(
        "timeout 120 " + GRPCURL + "hue.v1.EventService/Subscribe "
        "> /tmp/gap.json 2>/tmp/gap.err & echo started"
    )
    wait_for_subscriber(gateway, before)
    bridge.systemctl("stop fake-hue.service")
    gateway.sleep(5)
    bridge.systemctl("start fake-hue.service")
    bridge.wait_for_open_port(${toString bridgePort}, timeout=60)
    gateway.wait_until_succeeds(
        "grep -qF CAUSE_RECONNECTED /tmp/gap.json", timeout=90
    )
    gateway.wait_until_succeeds(
        GRPCURL + "hue.v1.LightingService/ListLights | grep -q Desk", timeout=30
    )

    # Step 10: the Application Key is nowhere in either node's journal.
    gateway.fail(f"journalctl --no-pager | grep -qF '{key}'")
    bridge.fail(f"journalctl --no-pager | grep -qF '{key}'")
    paired.fail(f"journalctl --no-pager | grep -qF '{minted_before}'")

    # And it never became an ExecStart argument.
    gateway.fail(
        "systemctl show hue-grpc.service -p ExecStart | grep -qF application-key"
    )
    gateway.succeed(
        "systemctl show hue-grpc.service -p ExecStart "
        "| grep -qF -- '--credentials-file'"
    )
  '';
}
