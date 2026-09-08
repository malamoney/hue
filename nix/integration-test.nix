# Issue #14: the gateway, in a booted NixOS VM, against a fake Bridge.
#
# Two nodes. `bridge` runs the fake Hue Bridge (nix/fake-hue.nix) with a
# certificate the gateway verifies for real. `gateway` runs the NixOS module
# in static-bridge mode, handed its Application Key the way a real deployment
# is — a Credentials File loaded by systemd `LoadCredential`, referenced by
# runtime path, never in the store or an `ExecStart` argument.
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

  # Minted once at build time. The gateway trusts this CA (bridge.caFile) and
  # the fake serves this leaf; the private key never leaves the `bridge` node.
  bridgeCerts = pkgs.runCommand "fake-hue-bridge-certs" { } ''
    ${fake-hue}/bin/fake-hue mint-certs --bridge-id ${bridgeId} --out $out
  '';

  credentialsPath = "/run/hue-grpc-secret/credentials";
  keyPath = "/run/fake-hue/application-key";
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
            ExecStart = builtins.concatStringsSep " " [
              "${fake-hue}/bin/fake-hue serve"
              "--bridge-id ${bridgeId}"
              "--listen-address 0.0.0.0 --port ${toString bridgePort}"
              "--cert-dir ${bridgeCerts}"
              "--application-key-file ${keyPath}"
              "--log"
            ];
            DynamicUser = true;
            RuntimeDirectory = "fake-hue";
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
            address = "bridge:${toString bridgePort}";
            id = bridgeId;
            credentialsFile = credentialsPath;
            caFile = "${bridgeCerts}/ca.pem";
          };
          # The event reconnect and resync are chatty; DEBUG is where the
          # redacted-header line lives, so it is also what step 10 checks.
          extraArgs = [
            "--log-level"
            "DEBUG"
          ];
        };

        # The test script writes the Credentials File and starts the service;
        # without this the unit would fail-loop at boot with nothing to load.
        systemd.services.hue-grpc.wantedBy = lib.mkForce [ ];
      };
  };

  testScript = ''
    start_all()

    # An Application Key that exists only in the running VMs.
    key = bridge.succeed("head -c 18 /dev/urandom | base64").strip()

    bridge.succeed("mkdir -p /run/fake-hue")
    bridge.succeed(f"printf '%s' '{key}' > ${keyPath}")
    bridge.systemctl("start fake-hue.service")
    bridge.wait_for_unit("fake-hue.service")
    bridge.wait_for_open_port(${toString bridgePort})

    # Step 3: the key reaches the gateway as a Credentials File, nothing more.
    gateway.succeed("install -d -m 0700 /run/hue-grpc-secret")
    gateway.succeed(f"install -m 0600 /dev/null ${credentialsPath}")
    gateway.succeed(f"printf 'application-key=%s' '{key}' > ${credentialsPath}")

    # Step 4 and 5: the unit starts and systemd calls it healthy.
    gateway.systemctl("start hue-grpc.service")
    gateway.wait_for_unit("hue-grpc.service")
    gateway.wait_for_open_port(50051)
    gateway.succeed("systemctl is-active hue-grpc.service")

    # Step 6: the standard gRPC health endpoint.
    gateway.succeed(
        "grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
        "grpc.health.v1.Health/Check | grep -q SERVING"
    )

    # Step 7a: one read. The fake serves two lights; find one to change.
    import json

    listed = json.loads(
        gateway.succeed(
            "grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
            "hue.v1.LightingService/ListLights"
        )
    )
    assert any(
        light["metadata"]["name"] == "Desk" for light in listed["lights"]
    ), listed
    light_id = listed["lights"][0]["id"]

    # Step 7b: one mutation.
    mutation = gateway.succeed(
        "grpcurl -plaintext -d "
        f"'{{\"lightId\":\"{light_id}\",\"command\":{{\"dimming\":{{\"brightness\":42}}}}}}' "
        "127.0.0.1:50051 hue.v1.LightingService/UpdateLight"
    )
    assert light_id in mutation, mutation

    # Step 7c: one event stream. Subscribe, change a light, see the change.
    gateway.succeed(
        "timeout 30 grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
        "hue.v1.EventService/Subscribe > /tmp/events.json 2>&1 & echo started"
    )
    gateway.sleep(3)
    gateway.succeed(
        "grpcurl -plaintext -d "
        f"'{{\"lightId\":\"{light_id}\",\"command\":{{\"dimming\":{{\"brightness\":77}}}}}}' "
        "127.0.0.1:50051 hue.v1.LightingService/UpdateLight"
    )
    gateway.wait_until_succeeds("grep -q '\"change\"' /tmp/events.json", timeout=20)

    # Step 8: restart, and the gateway still serves the same bridge with no
    # reconfiguration. Static config is the persisted state here — there is no
    # registry.json to check — and the state directory outlives the restart.
    gateway.succeed("test -d /var/lib/hue-grpc")
    gateway.systemctl("restart hue-grpc.service")
    gateway.wait_for_unit("hue-grpc.service")
    gateway.wait_for_open_port(50051)
    gateway.succeed("test -d /var/lib/hue-grpc")
    gateway.wait_until_succeeds(
        "grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
        "hue.v1.LightingService/ListLights | grep -q Desk",
        timeout=20,
    )

    # Step 9: the bridge drops off the network. The gRPC stream does not end;
    # the gateway reconnects and announces a Gap, then keeps serving.
    gateway.succeed(
        "timeout 90 grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
        "hue.v1.EventService/Subscribe > /tmp/gap.json 2>&1 & echo started"
    )
    gateway.sleep(3)
    bridge.systemctl("stop fake-hue.service")
    gateway.sleep(5)
    bridge.systemctl("start fake-hue.service")
    bridge.wait_for_open_port(${toString bridgePort})
    gateway.wait_until_succeeds(
        "grep -qiE 'gap|cause_reconnected' /tmp/gap.json", timeout=60
    )
    gateway.wait_until_succeeds(
        "grpcurl -plaintext -d '{}' 127.0.0.1:50051 "
        "hue.v1.LightingService/ListLights | grep -q Desk",
        timeout=30,
    )

    # Step 10: the Application Key is nowhere in the journal.
    gateway.fail(f"journalctl --no-pager | grep -qF '{key}'")
    bridge.fail(f"journalctl --no-pager | grep -qF '{key}'")

    # And it never became an ExecStart argument or an environment variable.
    gateway.fail(
        "systemctl show hue-grpc.service -p ExecStart | grep -qF application-key"
    )
    gateway.succeed(
        "systemctl show hue-grpc.service -p ExecStart "
        "| grep -qF -- '--credentials-file'"
    )
  '';
}
