# NixOS module for the Hue gRPC gateway.
#
# `services.hue-grpc.enable = true` produces a hardened systemd unit that runs
# the gateway. Non-secret settings are rendered into the unit's `ExecStart`,
# which lives in the immutable Nix store; the three secrets the service can
# need — the Application Key, the inbound TLS private key, and the Gateway
# Token — arrive only through systemd's `LoadCredential` and never touch the
# store, an `ExecStart` argument, or a Nix-rendered environment variable.
#
# `bridge.caFile` is config, not a secret — a CA certificate — so it is a
# plain `ExecStart` argument like the address and the Bridge ID.
#
# The sandbox below was tightened empirically against issue #14's VM test
# (issue #15): every directive is one the gateway keeps working without, kept
# honest by `systemd-analyze security` recording the score in that test while
# it stays green. Two directives are known landmines. `RestrictAddressFamilies`
# is default-deny of the AF_INET/AF_INET6 sockets the Bridge connection needs,
# failing with an error that reads as anything but a networking problem, so it
# stays at the three families the gateway actually opens. `MemoryDenyWriteExecute`
# can break Python C extensions, so it went in last and alone and is the first
# thing to back out if `grpcio` ever objects.
self:

{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.hue-grpc;

  # Matches the server's own rule: it decides loopback with Python's
  # ipaddress module, which is every 127.0.0.0/8 address plus ::1.
  isLoopback = lib.hasPrefix "127." cfg.listenAddress || cfg.listenAddress == "::1";

  hasBridge =
    cfg.bridge.address != null && cfg.bridge.id != null && cfg.bridge.credentialsFile != null;

  hasTls =
    cfg.grpc.tls.enable && cfg.grpc.tls.certificateFile != null && cfg.grpc.tls.privateKeyFile != null;

  hasToken = cfg.grpc.tokenFile != null;

  # LoadCredential drops each secret at $CREDENTIALS_DIRECTORY/<id>. %d is
  # systemd's specifier for that directory; it stays literal in the rendered
  # unit and systemd expands it when it starts the service.
  bridgeArgs = [
    "--bridge-address"
    cfg.bridge.address
    "--bridge-id"
    cfg.bridge.id
    "--credentials-file"
    "%d/credentials"
  ];

  # A CA cert is not a secret, so it is a plain ExecStart argument. It swaps
  # the trust anchor for the Bridge connection; the common-name check the
  # Gateway makes on top of it is unaffected. Rendered whether the Bridge is
  # given statically or read from a paired `registry.json`.
  caArgs = lib.optionals (cfg.bridge.caFile != null) [
    "--bridge-ca-file"
    cfg.bridge.caFile
  ];

  tlsArgs = [
    "--tls-certificate-file"
    cfg.grpc.tls.certificateFile
    "--tls-private-key-file"
    "%d/tls-key"
  ];

  tokenArgs = [
    "--gateway-token-file"
    "%d/gateway-token"
  ];

  serverArgs = [
    "--listen-address"
    cfg.listenAddress
    "--port"
    (toString cfg.port)
  ]
  ++ lib.optionals hasBridge bridgeArgs
  ++ caArgs
  ++ lib.optionals hasTls tlsArgs
  ++ lib.optionals hasToken tokenArgs
  ++ cfg.extraArgs;

  loadCredential =
    lib.optional hasBridge "credentials:${cfg.bridge.credentialsFile}"
    ++ lib.optional hasTls "tls-key:${cfg.grpc.tls.privateKeyFile}"
    ++ lib.optional hasToken "gateway-token:${cfg.grpc.tokenFile}";
in
{
  options.services.hue-grpc = {
    enable = lib.mkEnableOption "the Philips Hue gRPC gateway";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.hue-grpc;
      defaultText = lib.literalExpression "hue-grpc.packages.\${system}.hue-grpc";
      description = "The hue-grpc package to run.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        IP address the gRPC listener binds. Anything but loopback also needs
        {option}`services.hue-grpc.grpc.tls.enable` and
        {option}`services.hue-grpc.grpc.tokenFile`, the same rule the server
        enforces on startup.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 50051;
      description = "TCP port the gRPC listener binds.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open {option}`services.hue-grpc.port` in the host firewall. Off by
        default; a loopback listener needs nothing opened.
      '';
    };

    bridge.address = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "192.168.86.223";
      description = ''
        Address of the one Bridge to serve, given statically rather than
        discovered or paired. Setting this makes the service ignore any
        {file}`registry.json` and requires
        {option}`services.hue-grpc.bridge.id` and
        {option}`services.hue-grpc.bridge.credentialsFile`.
      '';
    };

    bridge.id = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "ECB5FAFFFE334703";
      description = ''
        The Bridge's permanent ID, asserted against the certificate it
        presents. Not a secret.
      '';
    };

    bridge.credentialsFile = lib.mkOption {
      # A runtime path string, never a path literal: a literal would be
      # copied into the world-readable Nix store, secrets and all.
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/hue-grpc";
      description = ''
        Path to a Credentials File: `key=value` lines holding `application-key`
        and, optionally, `client-key`. Loaded through systemd `LoadCredential`,
        so it can live under {file}`/run/secrets` or come from sops-nix or
        agenix; its contents never enter the Nix store. Referenced by runtime
        path only — never inlined into the configuration.
      '';
    };

    bridge.caFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/etc/hue-grpc/bridge-ca.pem";
      description = ''
        PEM CA the Bridge's certificate is verified against, instead of the
        Philips {file}`root-bridge` CA the package ships with. The Gateway's
        common-name check still runs on top; this only replaces the trust
        anchor, for a Bridge behind a certificate the Gateway was not
        shipped knowing about. Not a secret, and rendered straight into
        {env}`ExecStart`.
      '';
    };

    grpc.tls.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Serve gRPC over TLS. Required for a non-loopback listener.";
    };

    grpc.tls.certificateFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Path to the PEM certificate chain presented to gRPC clients. Not a
        secret, but a runtime path string for symmetry with the private key.
      '';
    };

    grpc.tls.privateKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Path to the PEM private key for
        {option}`services.hue-grpc.grpc.tls.certificateFile`. Loaded through
        systemd `LoadCredential`; never enters the Nix store.
      '';
    };

    grpc.tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Path to a file holding the Gateway Token clients must present.
        Required for a non-loopback listener. Loaded through systemd
        `LoadCredential`; never enters the Nix store or an `ExecStart`
        argument.
      '';
    };

    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "hue-grpc";
      description = ''
        Name under {file}`/var/lib` for the persistent Bridge registry.
        Created `0700` and owned by the service's `DynamicUser`.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "--log-format"
        "text"
      ];
      description = ''
        Extra arguments appended to the server command line — the way to
        reach `--reflection`, `--log-level`, `--event-queue-size` and the
        other operational flags the server documents but this module does
        not surface.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = isLoopback || cfg.grpc.tls.enable;
        message = "services.hue-grpc: a non-loopback listenAddress needs grpc.tls.enable = true.";
      }
      {
        assertion = isLoopback || cfg.grpc.tokenFile != null;
        message = "services.hue-grpc: a non-loopback listenAddress needs grpc.tokenFile.";
      }
      {
        assertion =
          cfg.grpc.tls.enable
          -> (cfg.grpc.tls.certificateFile != null && cfg.grpc.tls.privateKeyFile != null);
        message = "services.hue-grpc: grpc.tls.enable needs grpc.tls.certificateFile and grpc.tls.privateKeyFile.";
      }
      {
        assertion =
          (cfg.grpc.tls.certificateFile != null || cfg.grpc.tls.privateKeyFile != null)
          -> cfg.grpc.tls.enable;
        message = "services.hue-grpc: set grpc.tls.enable to use the TLS certificate and key.";
      }
      {
        assertion = (cfg.bridge.address != null) == (cfg.bridge.id != null);
        message = "services.hue-grpc: bridge.address and bridge.id go together.";
      }
      {
        assertion = (cfg.bridge.address != null) == (cfg.bridge.credentialsFile != null);
        message = "services.hue-grpc: bridge.address needs bridge.credentialsFile, and the reverse.";
      }
    ];

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.hue-grpc = {
      description = "Philips Hue gRPC gateway";
      documentation = [ "https://github.com/malamoney/hue" ];
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} ${lib.concatStringsSep " " serverArgs}";
        Type = "exec";
        Restart = "on-failure";
        RestartSec = "5s";

        LoadCredential = loadCredential;

        DynamicUser = true;
        StateDirectory = cfg.stateDirectory;
        StateDirectoryMode = "0700";

        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;

        # The gateway binds unprivileged ports and never changes uid, so it
        # needs no capabilities at all.
        CapabilityBoundingSet = "";
        SystemCallArchitectures = "native";
        UMask = "0077";

        # The curated service allowlist. A blocked call fails with EPERM rather
        # than a SIGSYS kill, so a future dependency that reaches for something
        # exotic degrades visibly instead of dying.
        SystemCallFilter = [ "@system-service" ];
        SystemCallErrorNumber = "EPERM";

        # The documented landmine. The default-deny set drops the AF_INET and
        # AF_INET6 sockets the Bridge connection needs; AF_UNIX is for the
        # journal. These three are the empirical floor: the VM test proves the
        # Bridge is still reachable, and no discovery means no AF_NETLINK.
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
      };
    };
  };
}
