#!/usr/local/bin/perl
use strict;
use warnings;
use WebminCore;
init_config();

# Load UI helpers and access control info
our %config;
our %text;
my $acl = get_module_acl();           # use ACLs if you add them (see section 4)

# -------- helpers --------

sub must_path {
    my ($key, $fallback) = @_;
    my $p = $config{$key} || $fallback;
    -x $p or &error("Configured path for $key ($p) is not executable");
    return $p;
}

sub shell_quote {
    # POSIX-safe single-quote escaping for arguments
    return join(' ', map { my $s=$_; $s =~ s/'/'"'"'/g; "'$s'"} @_);
}

sub run_cmd {
    my (@args) = @_;
    my $cmd = join(' ', @args);       # already quoted by caller
    my $out = &backquote_logged("$cmd 2>&1");   # logs in Webmin action log
    my $rc  = $? >> 8;
    return ($rc, $out);
}

sub size_h {
    my $b = shift // 0;
    my @u = qw(B KB MB GB TB PB);
    my $i=0; while ($b >= 1024 && $i < $#u) { $b/=1024; $i++ }
    return sprintf("%.1f %s", $b, $u[$i]);
}

sub list_geom_disks {
    # Parse: `geom disk list` for model/ident/mediasize
    my $geom = must_path('geom_cmd','/sbin/geom');
    my ($rc,$out) = run_cmd(shell_quote($geom), 'disk', 'list');
    $rc and &error("geom failed: $out");

    my @blocks = split(/\n\n+/, $out);
    my @res;
    for my $blk (@blocks) {
        my ($name) = $blk =~ /Name:\s*(\S+)/i;
        next unless $name;
        my ($mediasize) = $blk =~ /mediasize:\s*(\d+)/i;
        my ($descr)     = $blk =~ /descr:\s*(.+)\n/i;
        my ($ident)     = $blk =~ /ident:\s*(.+)\n/i;

        push @res, {
            name       => $name,                # e.g. ada0, da1, nda0
            dev        => "/dev/$name",
            mediasize  => $mediasize || 0,
            size_h     => size_h($mediasize||0),
            model      => (defined $descr ? $descr : ''),
            ident      => (defined $ident ? $ident : ''),
        };
    }
    return \@res;
}

sub map_gpart_status {
    # For each disk, detect: has table? scheme? any freebsd-zfs partition?
    my $gpart = must_path('gpart_cmd','/sbin/gpart');
    my ($rc,$out) = run_cmd(shell_quote($gpart), 'show', '-p');
    $rc and &error("gpart show failed: $out");

    my %info;
    my $cur;
    for my $line (split /\n/, $out) {
        if ($line =~ m{^=>\s+\d+\s+\d+\s+(\S+)\s+} ) {
            $cur = $1;                       # provider, e.g. /dev/ada0
            $info{$cur} ||= { scheme => undef, has_zfs_part => 0, lines => [] };
        }
        push @{$info{$cur}{lines}}, $line if $cur;

        if ($line =~ /(\S+)\s+GPT\s+/) {
            $info{$cur}{scheme} = 'gpt';
        } elsif ($line =~ /(\S+)\s+MBR\s+/) {
            $info{$cur}{scheme} = 'mbr';
        }
        if ($line =~ /\s+freebsd-zfs(\s|$)/) {
            $info{$cur}{has_zfs_part} = 1;
        }
    }
    return \%info;
}

sub detect_zfs_labels {
    # Use `zpool status` to map any devices currently part of a pool
    my $zpool = must_path('zpool_cmd','/sbin/zpool');
    my ($rc,$out) = run_cmd(shell_quote($zpool), 'status', '-L');
    my %in_pool;
    for my $line (split /\n/, $out) {
        if ($line =~ /^\s*(\/dev\/\S+|[a-z]+[0-9]+)\s+\d/ ) {
            my $dev = $1;
            $dev = "/dev/$dev" unless $dev =~ m{^/dev/};
            $in_pool{$dev} = 1;
        }
    }
    return \%in_pool;
}

sub main_ui {
    my $disks = list_geom_disks();
    my $gmap  = map_gpart_status();
    my $inpool= detect_zfs_labels();

    &ui_print_header(undef, 'ZFS Manager · Disks', '');
    print &ui_form_start("format.cgi", "post");

    print &ui_table_start("Detected disks", "width=100%");
    print &ui_table_row(
        '',
        "<b>Device</b> &nbsp; <b>Size</b> &nbsp; <b>Model</b> &nbsp; <b>Partitioning</b> &nbsp; <b>In pool?</b>"
    );

    for my $d (@$disks) {
        my $dev = $d->{dev};
        my $gp  = $gmap->{$dev} || {};
        my $scheme = $gp->{scheme} ? uc($gp->{scheme}) : '—';
        my $pzfs   = $gp->{has_zfs_part} ? 'yes' : 'no';
        my $in     = $inpool->{$dev} ? 'yes' : 'no';

        # Only allow selecting whole disks (adaX/daX/ndaX)
        my $selectable = ($dev =~ m{^/dev/(ada|da|nda)\d+$}) ? 1 : 0;

        my $cb = $selectable
          ? &ui_checkbox("dev", $dev, "", 0)
          : '&nbsp;';

        my $row = sprintf(
            "<tt>%s</tt> &nbsp; %s &nbsp; %s &nbsp; scheme:%s, freebsd-zfs:%s &nbsp; %s",
            $dev, $d->{size_h}, $d->{model} || '(unknown)', $scheme, $pzfs, $in
        );

        print &ui_table_row($cb, $row);
    }
    print &ui_table_end();

    # Options for formatting
    print &ui_table_start("Prepare selected disks for ZFS");
    print &ui_table_row("Label prefix",
        &ui_textbox("label_prefix", "zfsd", 20) . " &nbsp; (creates /dev/gpt/<prefix>NN)"
    );
    print &ui_table_row("Alignment", &ui_textbox("align", "1M", 8) . " &nbsp; (gpart -a)");
    print &ui_table_row("Wipe old ZFS labels",
        &ui_yesno_radio("wipe_labels", 1)
    );
    print &ui_table_row("Safety check",
        "Type the number of selected disks to confirm: " .
        &ui_textbox("confirm_count", "", 5)
    );

    print &ui_table_end();
    print &ui_submit("Prepare for ZFS");
    print &ui_form_end();

    print &ui_hr();
    print &ui_links_row([ [ "create.cgi", "Back to Create Pool" ] ]);
    &ui_print_footer("/", "Return to Webmin");
}

# -------- main --------
main_ui();
exit 0;
