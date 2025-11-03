#!/usr/local/bin/perl
use strict;
use warnings;
use WebminCore;
init_config();

our %config;
my $acl = get_module_acl();     # see ACL section below

# ---- helpers (shared approach) ----
sub must_path {
    my ($key, $fallback) = @_;
    my $p = $config{$key} || $fallback;
    -x $p or &error("Configured path for $key ($p) is not executable");
    return $p;
}
sub shell_quote { return join(' ', map { my $s=$_; $s =~ s/'/'"'"'/g; "'$s'"} @_) }
sub run_cmd {
    my (@args) = @_;
    my $cmd = join(' ', @args);
    my $out = &backquote_logged("$cmd 2>&1");
    my $rc  = $? >> 8;
    return ($rc, $out);
}
sub is_whole_device_ok {
    my ($dev) = @_;
    return ($dev && $dev =~ m{^/dev/(ada|da|nda)\d+$});
}
sub next_free_gpt_label {
    my ($prefix) = @_;
    my $i = 1;
    while (1) {
        my $lbl = sprintf("%s%02d", $prefix, $i);
        return $lbl unless -e "/dev/gpt/$lbl";
        $i++;
        &error("Excessive label search") if $i > 9999;
    }
}

# ---- format worker ----
sub prepare_one_disk {
    my ($dev, $opt) = @_;
    my $gpart = must_path('gpart_cmd','/sbin/gpart');
    my $zpool = must_path('zpool_cmd','/sbin/zpool');

    # 0) optional: wipe stray ZFS labels
    if ($opt->{wipe_labels}) {
        my ($rc,$out) = run_cmd(shell_quote($zpool), 'labelclear', '-f', shell_quote($dev));
        # labelclear fails if no label; treat non-zero only if message isn't "bad label"
        if ($rc) {
            # not fatal; continue
        }
    }

    # 1) remove any existing partitioning (ignore error if none)
    run_cmd(shell_quote($gpart), 'destroy', '-F', shell_quote($dev));

    # 2) create GPT
    my ($rc_c,$out_c) = run_cmd(shell_quote($gpart), 'create', '-s', 'gpt', shell_quote($dev));
    $rc_c and &error("gpart create failed for $dev: $out_c");

    # 3) add freebsd-zfs slice with unique label
    my $label = next_free_gpt_label($opt->{label_prefix});
    my ($rc_a,$out_a) = run_cmd(
        shell_quote($gpart), 'add',
        '-a', shell_quote($opt->{align} || '1M'),
        '-t', 'freebsd-zfs',
        '-l', shell_quote($label),
        shell_quote($dev)
    );
    $rc_a and &error("gpart add failed for $dev: $out_a");

    my $gpt_path = "/dev/gpt/$label";
    return ($gpt_path, $label);
}

# ---- UI entrypoint ----
sub main {
    # 1) ACL: block if user lacks permission
    if ($acl && $acl->{'can_format'} && $acl->{'can_format'} ne '1') {
        &error("You are not permitted to format disks in this module.");
    }

    # 2) Collect POST
    my @devs = &cgi_multiple_param('dev');           # multiple "dev" fields
    my $label_prefix = &cgi_param('label_prefix') || 'zfsd';
    my $align        = &cgi_param('align') || '1M';
    my $wipe_labels  = &cgi_param('wipe_labels') ? 1 : 0;
    my $confirm      = &cgi_param('confirm_count');

    # 3) Basic validation
    @devs = grep { is_whole_device_ok($_) } @devs;
    @devs or &error("No valid whole-disk devices were selected (adaX/daX/ndaX only).");

    $confirm =~ /^\d+$/ or &error("Confirmation must be a number.");
    $confirm == scalar(@devs) or &error("Confirmation mismatch: you typed $confirm, but selected ".scalar(@devs)." disks.");

    $label_prefix =~ /^[A-Za-z0-9._-]{1,16}$/
      or &error("Label prefix contains invalid characters.");

    $align =~ /^[0-9]+[KMGTP]?B?$/i
      or &error("Alignment must be like 1M, 4K, 1048576, etc.");

    # 4) Dangerous banner
    &ui_print_header(undef, 'Preparing disks for ZFS', '');
    print &ui_hr();
    print "<div class=msg>About to (optionally) wipe ZFS labels, destroy any partitioning, create GPT, and add a freebsd-zfs slice on the selected devices.</div>\n";

    # 5) Execute
    my @newlabels;
    for my $d (@devs) {
        print "<h3><tt>$d</tt></h3>\n";
        my ($path,$lbl) = prepare_one_disk($d, {
            wipe_labels  => $wipe_labels,
            label_prefix => $label_prefix,
            align        => $align,
        });
        push @newlabels, $path;
        print "<div>Created: <tt>$path</tt></div>\n";
        print &ui_hr();
    }

    # 6) Shortcut back to Create Pool with devices pre-selected
    print &ui_form_start("create.cgi", "post");
    for my $p (@newlabels) {
        print &ui_hidden("newdev", $p);
        print "<div>Ready: <tt>$p</tt></div>\n";
    }
    print &ui_submit("Use these in a new pool");
    print &ui_form_end();

    &webmin_log("format_for_zfs", undef, join(' ',@devs));
    &ui_print_footer("/", "Return to Webmin");
}
main();
exit 0;
