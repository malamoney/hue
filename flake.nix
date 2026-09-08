{
  description = "Gateway exposing a subset of the Philips Hue CLIP v2 API over gRPC";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      # Development happens on aarch64-darwin; the service is deployed to
      # x86_64-linux (a 2011 MacBook Pro running NixOS). Both must evaluate.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];

      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        hue-grpc = pkgs.python312Packages.callPackage ./nix/package.nix { };
        default = hue-grpc;
      });

      apps = forAllSystems (pkgs: rec {
        hue-grpc-server = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.system}.hue-grpc;
        };
        default = hue-grpc-server;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python312.withPackages (ps: [
              ps.grpcio
              ps.grpcio-health-checking
              ps.grpcio-reflection
              ps.grpcio-tools
              ps.protobuf
              ps.httpx
              ps.cryptography
              ps.pytest
              ps.mypy
              ps.pyyaml
            ]))
            pkgs.protobuf
            pkgs.ruff
            # For checking reflection by hand: grpcurl -plaintext 127.0.0.1:50051 list
            pkgs.grpcurl
          ];

          shellHook = ''
            # pyproject deliberately sets no pytest pythonpath, so that the
            # Nix check phase tests the installed package rather than ./src.
            export PYTHONPATH="$PWD/src:$PWD/tools''${PYTHONPATH:+:$PYTHONPATH}"
            echo "hue-grpc dev shell - pytest, ruff, mypy, protoc available"
          '';
        };
      });

      checks = forAllSystems (
        pkgs:
        {
          # Building the package runs the test suite via pytestCheckHook.
          package = self.packages.${pkgs.system}.hue-grpc;

          lint = pkgs.runCommand "hue-grpc-lint" { nativeBuildInputs = [ pkgs.ruff ]; } ''
            cd ${self}
            ruff check --no-cache .
            ruff format --no-cache --check .
            touch $out
          '';

          format = pkgs.runCommand "hue-grpc-format" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
            cd ${self}
            # Passing a directory to nixfmt is deprecated, so enumerate files.
            find . -name '*.nix' -exec nixfmt --check {} +
            touch $out
          '';

          protogen =
            pkgs.runCommand "hue-grpc-protogen"
              {
                nativeBuildInputs = [
                  (pkgs.python312.withPackages (ps: [
                    ps.pytest
                    ps.pyyaml
                  ]))
                  pkgs.protobuf
                ];
              }
              ''
                cd ${self}
                export PYTHONPATH="$PWD/tools"
                export PYTHONDONTWRITEBYTECODE=1
                pytest tests/protogen -q -p no:cacheprovider
                touch $out
              '';

          # The committed .proto files must match what the generator produces,
          # and protoc must accept them. A stale checked-in proto would
          # otherwise diverge silently from the spec it claims to come from.
          protos-current =
            pkgs.runCommand "hue-grpc-protos-current"
              {
                nativeBuildInputs = [
                  (pkgs.python312.withPackages (ps: [ ps.pyyaml ]))
                  pkgs.protobuf
                  pkgs.diffutils
                ];
              }
              ''
                cp -r ${self} source
                chmod -R +w source
                cd source

                export PYTHONPATH="$PWD/tools"
                export PYTHONDONTWRITEBYTECODE=1

                # Take out everything the generator claims, so a file the
                # manifest no longer produces shows up as a difference rather
                # than surviving untouched. Hand-written definitions — the
                # services, which OpenAPI has no notion of — say so in their
                # first line and stay where they are.
                grep -rl 'Generated from the Hue OpenAPI document' proto/hue \
                  | while read -r generated; do rm "$generated"; done
                python -m protogen --manifest proto/manifest.toml

                diff -ru ${self}/proto ./proto
                protoc --proto_path=proto --descriptor_set_out=/dev/null \
                  proto/hue/v1/*.proto

                touch $out
              '';

          # Issue #9's acceptance, run against the real binary rather than a
          # test harness: the server starts, health answers, and a client that
          # knows nothing about this project can list its services through
          # reflection.
          acceptance =
            pkgs.runCommand "hue-grpc-acceptance"
              {
                nativeBuildInputs = [
                  self.packages.${pkgs.system}.hue-grpc
                  pkgs.grpcurl
                ];
              }
              ''
                port=50251
                hue-grpc-server --port "$port" --log-format json >server.log 2>&1 &
                gateway=$!
                trap 'kill $gateway 2>/dev/null || true' EXIT

                # The listener is up within milliseconds; twenty seconds is for
                # a loaded builder, not for a server that is going to fail.
                for _ in $(seq 1 100); do
                  if grpcurl -plaintext "127.0.0.1:$port" list >services.txt 2>/dev/null; then
                    break
                  fi
                  sleep 0.2
                done
                cat server.log

                grep -q 'grpc.health.v1.Health' services.txt
                grep -q 'grpc.reflection.v1alpha.ServerReflection' services.txt
                grep -q 'hue.v1.LightingService' services.txt
                grpcurl -plaintext -d '{}' "127.0.0.1:$port" \
                  grpc.health.v1.Health/Check | grep -q SERVING

                # Issue #10's service, on a gateway that has never paired: it
                # answers, and what it answers is what to do about that.
                if grpcurl -plaintext -d '{}' "127.0.0.1:$port" \
                  hue.v1.LightingService/ListLights >lighting.txt 2>&1; then
                  echo "an unpaired gateway listed lights" >&2
                  exit 1
                fi
                grep -q 'FailedPrecondition' lighting.txt
                grep -q 'pair' lighting.txt

                # systemd stops the unit this way, and expects exit 0.
                kill -TERM $gateway
                wait $gateway
                grep -q 'gateway stopped' server.log

                touch $out
              '';

          typecheck =
            pkgs.runCommand "hue-grpc-typecheck"
              {
                nativeBuildInputs = [
                  (pkgs.python312.withPackages (ps: [
                    ps.mypy
                    ps.pytest
                    ps.pyyaml
                    ps.grpcio
                    ps.grpcio-health-checking
                    ps.grpcio-reflection
                    ps.protobuf
                    ps.httpx
                    ps.cryptography
                  ]))
                ];
              }
              ''
                cd ${self}
                export MYPY_CACHE_DIR="$TMPDIR/mypy"
                mypy
                touch $out
              '';
        }
        // nixpkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # Issue #13's acceptance short of booting a VM (that is issue #14):
          # the module evaluates both ways round, and the unit it generates
          # carries the named hardening set, the address-family landmine fix,
          # and secrets that arrive only by LoadCredential and runtime path.
          nixos-module =
            let
              unitOf =
                extra:
                (nixpkgs.lib.nixosSystem {
                  inherit (pkgs) system;
                  modules = [
                    self.nixosModules.default
                    {
                      boot.isContainer = true;
                      system.stateVersion = "26.05";
                      services.hue-grpc = {
                        enable = true;
                      }
                      // extra;
                    }
                  ];
                }).config.systemd.units."hue-grpc.service".unit;

              configured = unitOf {
                bridge = {
                  address = "192.168.86.223";
                  id = "ECB5FAFFFE334703";
                  credentialsFile = "/run/secrets/hue-grpc";
                };
              };
              bare = unitOf { };
            in
            pkgs.runCommand "hue-grpc-nixos-module" { } ''
              configured="${configured}/hue-grpc.service"
              bare="${bare}/hue-grpc.service"
              echo "=== configured ==="; cat "$configured"
              echo "=== bare ===";       cat "$bare"

              want() {
                grep -qF -- "$2" "$1" || { echo "$1 is missing: $2" >&2; exit 1; }
              }
              deny() {
                if grep -qF -- "$2" "$1"; then
                  echo "$1 should not have: $2" >&2
                  exit 1
                fi
              }

              for service in "$configured" "$bare"; do
                want "$service" 'DynamicUser=true'
                want "$service" 'StateDirectory=hue-grpc'
                want "$service" 'StateDirectoryMode=0700'
                want "$service" 'NoNewPrivileges=true'
                want "$service" 'PrivateTmp=true'
                want "$service" 'ProtectSystem=strict'
                want "$service" 'ProtectHome=true'
                want "$service" 'ProtectKernelTunables=true'
                want "$service" 'ProtectKernelModules=true'
                want "$service" 'ProtectControlGroups=true'
                want "$service" 'Restart=on-failure'
                # systemd unions repeated RestrictAddressFamilies= lines, and
                # NixOS renders the list one family per line.
                want "$service" 'RestrictAddressFamilies=AF_INET'
                want "$service" 'RestrictAddressFamilies=AF_INET6'
                want "$service" 'RestrictAddressFamilies=AF_UNIX'
              done

              # Configured: the secret arrives as a credential, is referenced by
              # the %d runtime path, and its source is never rendered as an arg.
              want "$configured" 'LoadCredential=credentials:/run/secrets/hue-grpc'
              want "$configured" '--credentials-file %d/credentials'
              want "$configured" '--bridge-address 192.168.86.223'
              want "$configured" '--bridge-id ECB5FAFFFE334703'

              # Bare enable: a running listener, no bridge, nothing to load.
              deny "$bare" '--bridge-address'
              deny "$bare" 'LoadCredential='

              touch $out
            '';
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt);

      nixosModules = rec {
        hue-grpc = import ./nix/module.nix self;
        default = hue-grpc;
      };
    };
}
