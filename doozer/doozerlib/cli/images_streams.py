import io
import os
import click
import yaml
import json
import hashlib
import time

from github import Github, UnknownObjectException, GithubException
from dockerfile_parse import DockerfileParser

from dockerfile_parse import DockerfileParser
from doozerlib.model import Missing
from doozerlib.pushd import Dir
from doozerlib.cli import cli, pass_runtime
from doozerlib import brew, state, exectools
from doozerlib.util import get_docker_config_json, convert_remote_git_to_ssh, \
    split_git_url, remove_prefix, green_print,\
    yellow_print, red_print, convert_remote_git_to_https, \
    what_is_in_master, extract_version_fields, convert_remote_git_to_https


@cli.group("images:streams", short_help="Manage ART equivalent images in upstream CI.")
def images_streams():
    """
    When changing streams.yml, the following sequence of operations is required.

    \b
    1. Set KUBECONFIG to the art-publish service account.
    2. Run gen-buildconfigs with --apply or 'oc apply' it after the fact.
    3. Run mirror verb to push the images to the api.ci cluster (consider if --only-if-missing is appropriate).
    4. Run start-builds to trigger buildconfigs if they have not run already.
    5. Run 'check-upstream' after about 10 minutes to make sure those builds have succeeded. Investigate any failures.
    6. Run 'prs open' when check-upstream indicates all builds are complete.

    To test changes before affecting CI:

    \b
    - You can run, 'gen-buildconfigs', 'start-builds', and 'check-upstream' with the --live-test-mode flag.
      This will create and run real buildconfigs on api.ci, but they will not promote into locations used by CI.
    - You can run 'prs open' with --moist-run. This will create forks for the target repos, but will only
      print out PRs that would be opened instead of creating them.

    """
    pass


@images_streams.command('mirror', short_help='Reads streams.yml and mirrors out ART equivalent images to api.ci.')
@click.option('--stream', 'streams', metavar='STREAM_NAME', default=[], multiple=True, help='If specified, only these stream names will be mirrored.')
@click.option('--only-if-missing', default=False, is_flag=True, help='Only mirror the image if there is presently no equivalent image upstream.')
@click.option('--live-test-mode', default=False, is_flag=True, help='Append "test" to destination images to exercise live-test-mode buildconfigs')
@click.option('--dry-run', default=False, is_flag=True, help='Do not build anything, but only print build operations.')
@pass_runtime
def images_streams_mirror(runtime, streams, only_if_missing, live_test_mode, dry_run):
    runtime.initialize(clone_distgits=False, clone_source=False)
    runtime.assert_mutation_is_permitted()

    if streams:
        user_specified = True
    else:
        user_specified = False
        streams = runtime.get_stream_names()

    streams_config = runtime.streams
    for stream in streams:
        if streams_config[stream] is Missing:
            raise IOError(f'Did not find stream {stream} in streams.yml for this group')

        config = streams_config[stream]
        if config.mirror is True or user_specified:
            upstream_dest = config.upstream_image
            if upstream_dest is Missing:
                raise IOError(f'Unable to mirror stream {stream} as upstream_image is not defined')

            # If the configuration specifies a upstream_image_base, then ART is responsible for mirroring
            # that location and NOT the upstream_image. DPTP will take the upstream_image_base and
            # formulate the upstream_image.
            if config.upstream_image_base is not Missing:
                upstream_dest = config.upstream_image_base

            brew_image = config.image
            brew_pullspec = runtime.resolve_brew_image_url(brew_image)

            if only_if_missing:
                check_cmd = f'oc image info {upstream_dest}'
                rc, check_out, check_err = exectools.cmd_gather(check_cmd)
                if rc == 0:
                    print(f'Image {upstream_dest} seems to exist already; skipping because of --only-if-missing')
                    continue

            if live_test_mode:
                upstream_dest += '.test'

            cmd = f'oc image mirror {brew_pullspec} {upstream_dest}'

            if runtime.registry_config_dir is not None:
                cmd += f" --registry-config={get_docker_config_json(runtime.registry_config_dir)}"
            if dry_run:
                print(f'For {stream}, would have run: {cmd}')
            else:
                exectools.cmd_assert(cmd, retries=3, realtime=True)


@images_streams.command('check-upstream', short_help='Dumps information about CI buildconfigs/mirrored images associated with this group.')
@click.option('--live-test-mode', default=False, is_flag=True, help='Scan for live-test mode buildconfigs')
@pass_runtime
def images_streams_check_upstream(runtime, live_test_mode):
    runtime.initialize(clone_distgits=False, clone_source=False)

    streams = runtime.get_stream_names()
    streams_config = runtime.streams
    istags_status = []
    for stream in streams:
        config = streams_config[stream]

        if not(config.transform or config.mirror):
            continue

        upstream_dest = config.upstream_image
        _, dest_ns, dest_istag = upstream_dest.rsplit('/', maxsplit=2)

        if live_test_mode:
            dest_istag += '.test'

        rc, stdout, stderr = exectools.cmd_gather(f'oc get -n {dest_ns} istag {dest_istag} --no-headers')
        if rc:
            istags_status.append(f'ERROR: {stream}\nIs not yet represented upstream in {dest_ns} istag/{dest_istag}')
        else:
            istags_status.append(f'OK: {stream} exists, but check whether it is recently updated\n{stdout}')

    group_label = runtime.group_config.name
    if live_test_mode:
        group_label += '.test'

    bc_stdout, bc_stderr = exectools.cmd_assert(f'oc -n ci get -o=wide buildconfigs -l art-builder-group={group_label}')
    builds_stdout, builds_stderr = exectools.cmd_assert(f'oc -n ci get -o=wide builds -l art-builder-group={group_label}')
    ds_stdout, ds_stderr = exectools.cmd_assert(f'oc -n ci get ds -l art-builder-group={group_label}')
    print('Daemonset status (pins image to prevent gc on node; verify that READY=CURRENT):')
    print(ds_stdout or ds_stderr)
    print('Build configs:')
    print(bc_stdout or bc_stderr)
    print('Recent builds:')
    print(builds_stdout or builds_stderr)

    print('Upstream imagestream tag status')
    for istag_status in istags_status:
        print(istag_status)
        print()


@images_streams.command('start-builds', short_help='Triggers a build for each buildconfig associated with this group.')
@click.option('--as', 'as_user', metavar='CLUSTER_USERNAME', required=False, default=None, help='Specify --as during oc start-build')
@click.option('--live-test-mode', default=False, is_flag=True, help='Act on live-test mode buildconfigs')
@pass_runtime
def images_streams_start_buildconfigs(runtime, as_user, live_test_mode):
    runtime.initialize(clone_distgits=False, clone_source=False)

    group_label = runtime.group_config.name
    if live_test_mode:
        group_label += '.test'

    cmd = f'oc -n ci get -o=name buildconfigs -l art-builder-group={group_label}'
    if as_user:
        cmd += f' --as {as_user}'
    bc_stdout, bc_stderr = exectools.cmd_assert(cmd, retries=3)
    bc_stdout = bc_stdout.strip()

    if bc_stdout:
        for name in bc_stdout.splitlines():
            print(f'Triggering: {name}')
            cmd = f'oc -n ci start-build {name}'
            if as_user:
                cmd += f' --as {as_user}'
            stdout, stderr = exectools.cmd_assert(cmd, retries=3)
            print('   ' + stdout or stderr)
    else:
        print(f'No buildconfigs associated with this group: {group_label}')


@images_streams.command('gen-buildconfigs', short_help='Generates buildconfigs necessary to assemble ART equivalent images upstream.')
@click.option('--stream', 'streams', metavar='STREAM_NAME', default=[], multiple=True, help='If specified, only these stream names will be processed.')
@click.option('-o', '--output', metavar='FILENAME', required=True, help='The filename into which to write the YAML. It should be oc applied against api.ci as art-publish. The file may be empty if there are no buildconfigs.')
@click.option('--as', 'as_user', metavar='CLUSTER_USERNAME', required=False, default=None, help='Specify --as during oc apply')
@click.option('--apply', default=False, is_flag=True, help='Apply the output if any buildconfigs are generated')
@click.option('--live-test-mode', default=False, is_flag=True, help='Generate live-test mode buildconfigs')
@pass_runtime
def images_streams_gen_buildconfigs(runtime, streams, output, as_user, apply, live_test_mode):
    """
    ART has a mandate to make "ART equivalent" images available usptream for CI workloads. This enables
    CI to compile with the same golang version ART is using and use identical UBI8 images, etc. To accomplish
    this, streams.yml contains metadata which is extraneous to the product build, but critical to enable
    a high fidelity CI signal.
    It may seem at first that all we would need to do was mirror the internal brew images we use
    somewhere accessible by CI, but it is not that simple:
    1. When a CI build yum installs, it needs to pull RPMs from an RPM mirroring service that runs in
       CI. That mirroring service subsequently pulls and caches files ART syncs using reposync.
    2. There is a variation of simple golang builders that CI uses to compile test cases. These
       images are configured in ci-operator config's 'build_root' and they are used to build
       and run test cases. Sometimes called 'CI release' image, these images contain tools that
       are not part of the typical golang builder (e.g. tito).
    Both of these differences require us to 'transform' the image ART uses in brew into an image compatible
    for use in CI. A challenge to this transformation is that they must be performed in the CI context
    as they depend on the services only available in ci (e.g. base-4-6-rhel8.ocp.svc is used to
    find the current yum repo configuration which should be used).
    To accomplish that, we don't build the images ourselves. We establish OpenShift buildconfigs on the CI
    cluster which process intermediate images we push into the final, CI consumable image.
    These buildconfigs are generated dynamically by this sub-verb.
    The verb will also create a daemonset for the group on the CI cluster. This daemonset overcomes
    a bug in OpenShift 3.11 where the kubelet can garbage collection an image that the build process
    is about to use (because the kubelet & build do not communicate). The daemonset forces the kubelet
    to know the image is in use. These daemonsets can like be eliminated when CI infra moves fully to
    4.x.
    """
    runtime.initialize(clone_distgits=False, clone_source=False)
    runtime.assert_mutation_is_permitted()

    if not streams:
        # If not specified, use all.
        streams = runtime.get_stream_names()

    transform_rhel_7_base_repos = 'rhel-7/base-repos'
    transform_rhel_8_base_repos = 'rhel-8/base-repos'
    transform_rhel_7_golang = 'rhel-7/golang'
    transform_rhel_8_golang = 'rhel-8/golang'
    transform_rhel_7_ci_build_root = 'rhel-7/ci-build-root'
    transform_rhel_8_ci_build_root = 'rhel-8/ci-build-root'

    # The set of valid transforms
    transforms = set([
        transform_rhel_7_base_repos,
        transform_rhel_8_base_repos,
        transform_rhel_7_golang,
        transform_rhel_8_golang,
        transform_rhel_7_ci_build_root,
        transform_rhel_8_ci_build_root,
    ])

    major = runtime.group_config.vars['MAJOR']
    minor = runtime.group_config.vars['MINOR']

    rpm_repos_conf = runtime.group_config.repos or {}

    group_label = runtime.group_config.name
    if live_test_mode:
        group_label += '.test'

    buildconfig_definitions = []
    ds_container_definitions = []
    streams_config = runtime.streams
    for stream in streams:
        if streams_config[stream] is Missing:
            raise IOError(f'Did not find stream {stream} in streams.yml for this group')

        config = streams_config[stream]

        transform = config.transform
        if transform is Missing:
            # No buildconfig is necessary
            continue

        if transform not in transforms:
            raise IOError(f'Unable to render buildconfig for stream {stream} - transform {transform} not found within {transforms}')

        upstream_dest = config.upstream_image
        upstream_intermediate_image = config.upstream_image_base
        if upstream_dest is Missing or upstream_intermediate_image is Missing:
            raise IOError(f'Unable to render buildconfig for stream {stream} - you must define upstream_image_base AND upstream_image')

        # split a pullspec like registry.svc.ci.openshift.org/ocp/builder:rhel-8-golang-openshift-{MAJOR}.{MINOR}.art
        # into  OpenShift namespace, imagestream, and tag
        _, intermediate_ns, intermediate_istag = upstream_intermediate_image.rsplit('/', maxsplit=2)
        if live_test_mode:
            intermediate_istag += '.test'
        intermediate_imagestream, intermediate_tag = intermediate_istag.split(':')

        _, dest_ns, dest_istag = upstream_dest.rsplit('/', maxsplit=2)
        if live_test_mode:
            dest_istag += '.test'
        dest_imagestream, dest_tag = dest_istag.split(':')

        python_file_dir = os.path.dirname(__file__)

        # should align with files like: doozerlib/cli/streams_transforms/rhel-7/base-repos
        transform_template = os.path.join(python_file_dir, 'streams_transforms', transform, 'Dockerfile')
        with open(transform_template, mode='r', encoding='utf-8') as tt:
            transform_template_content = tt.read()

        dfp = DockerfileParser(cache_content=True, fileobj=io.BytesIO())
        dfp.content = transform_template_content

        # Make sure that upstream images can discern they are building in CI with ART equivalent images
        dfp.envs['OPENSHIFT_CI'] = 'true'

        dfp.labels['io.k8s.display-name'] = f'{dest_imagestream}-{dest_tag}'
        dfp.labels['io.k8s.description'] = f'ART equivalent image {group_label}-{stream} - {transform}'

        def add_localdev_repo_profile(profile):
            """
            The images we push to CI are used in two contexts:
            1. In CI builds, running on the CI clusters.
            2. In local development (e.g. docker build).
            This method is enabling the latter. If a developer is connected to the VPN,
            they will not be able to resolve RPMs through the RPM mirroring service running
            on CI, but they will be able to pull RPMs directly from the sources ART does.
            Since skip_if_unavailable is True globally, it doesn't matter if they can't be
            accessed via CI.
            """
            for repo_name in rpm_repos_conf.keys():
                repo_desc = rpm_repos_conf[repo_name]
                localdev_repo_name = f'localdev-{repo_name}'
                repo_conf = repo_desc.conf
                ci_alignment = repo_conf.ci_alignment
                if ci_alignment.localdev.enabled and profile in ci_alignment.profiles:
                    # CI only really deals with with x86_64 at this time.
                    if repo_conf.baseurl.unsigned:
                        x86_64_url = repo_conf.baseurl.unsigned.x86_64
                    else:
                        x86_64_url = repo_conf.baseurl.x86_64
                    if not x86_64_url:
                        raise IOError(f'Expected x86_64 baseurl for repo {repo_name}')
                    dfp.add_lines(f"RUN echo -e '[{localdev_repo_name}]\\nname = {localdev_repo_name}\\nid = {localdev_repo_name}\\nbaseurl = {x86_64_url}\\nenabled = 1\\ngpgcheck = 0\\n' > /etc/yum.repos.d/{localdev_repo_name}.repo")

        if transform == transform_rhel_8_base_repos or config.transform == transform_rhel_8_golang:
            # The repos transform create a build config that will layer the base image with CI appropriate yum
            # repository definitions.
            dfp.add_lines(f'RUN rm -rf /etc/yum.repos.d/*.repo && curl http://base-{major}-{minor}-rhel8.ocp.svc > /etc/yum.repos.d/ci-rpm-mirrors.repo')

            # Allow the base repos to be used BEFORE art begins mirroring 4.x to openshift mirrors.
            # This allows us to establish this locations later -- only disrupting CI for those
            # components that actually need reposync'd RPMs from the mirrors.
            dfp.add_lines('RUN yum config-manager --setopt=skip_if_unavailable=True --save')
            add_localdev_repo_profile('el8')

        if transform == transform_rhel_7_base_repos or config.transform == transform_rhel_7_golang:
            # The repos transform create a build config that will layer the base image with CI appropriate yum
            # repository definitions.
            dfp.add_lines(f'RUN rm -rf /etc/yum.repos.d/*.repo && curl http://base-{major}-{minor}.ocp.svc > /etc/yum.repos.d/ci-rpm-mirrors.repo')
            # Allow the base repos to be used BEFORE art begins mirroring 4.x to openshift mirrors.
            # This allows us to establish this locations later -- only disrupting CI for those
            # components that actually need reposync'd RPMs from the mirrors.
            add_localdev_repo_profile('el7')
            dfp.add_lines("RUN yum-config-manager --save '--setopt=skip_if_unavailable=True'")
            dfp.add_lines("RUN yum-config-manager --save '--setopt=*.skip_if_unavailable=True'")

        # We've arrived at a Dockerfile.
        dockerfile_content = dfp.content

        # Now to create a buildconfig for it.
        buildconfig = {
            'apiVersion': 'v1',
            'kind': 'BuildConfig',
            'metadata': {
                'name': f'{dest_imagestream}-{dest_tag}--art-builder',
                'namespace': 'ci',
                'labels': {
                    'art-builder-group': group_label,
                    'art-builder-stream': stream,
                },
                'annotations': {
                    'description': 'Generated by the ART pipeline by doozer. Processes raw ART images into ART equivalent images for CI.'
                }
            },
            'spec': {
                'failedBuildsHistoryLimit': 2,
                'output': {
                    'to': {
                        'kind': 'ImageStreamTag',
                        'namespace': dest_ns,
                        'name': dest_istag
                    }
                },
                'source': {
                    'dockerfile': dockerfile_content,
                    'type': 'Dockerfile'
                },
                'strategy': {
                    'dockerStrategy': {
                        'from': {
                            'kind': 'ImageStreamTag',
                            'name': intermediate_istag,
                            'namespace': intermediate_ns,
                        },
                        'imageOptimizationPolicy': 'SkipLayers',
                    },
                },
                'successfulBuildsHistoryLimit': 2,
                'triggers': [{
                    'imageChange': {},
                    'type': 'ImageChange'
                }]
            }
        }

        buildconfig_definitions.append(buildconfig)

        # define a daemonset container that will keep this image running on all nodes so that it will
        # not be garbage collected by the kubelet in 3.11.
        ds_container_definitions.append({
            "image": f"registry.svc.ci.openshift.org/{dest_ns}/{dest_istag}",
            "command": [
                "/bin/bash",
                "-c",
                "#!/bin/bash\nset -euo pipefail\ntrap 'jobs -p | xargs -r kill || true; exit 0' TERM\nwhile true; do\n  sleep 600 &\n  wait $!\ndone\n"
            ],
            "name": f"{dest_ns}-{dest_imagestream}-{dest_tag}".replace('.', '-'),
            "resources": {
                    "requests": {
                        "cpu": "50m"
                    }
            },
            "imagePullPolicy": "Always"
        })

    with open(output, mode='w+', encoding='utf-8') as f:
        objects = list()
        objects.extend(buildconfig_definitions)
        yaml.dump_all(objects, f, default_flow_style=False)

    if apply:
        if buildconfig_definitions:
            print('Applying buildconfigs...')
            cmd = f'oc apply -f {output}'
            if as_user:
                cmd += f' --as {as_user}'
            exectools.cmd_assert(cmd, retries=3)
        else:
            print('No buildconfigs were generated; skipping apply.')


def calc_parent_digest(parent_images):
    m = hashlib.md5()
    m.update(';'.join(parent_images).encode('utf-8'))
    return m.hexdigest()


def extract_parent_digest(dockerfile_path):
    with dockerfile_path.open(mode='r') as handle:
        dfp = DockerfileParser(cache_content=True, fileobj=io.BytesIO())
        dfp.content = handle.read()
    return calc_parent_digest(dfp.parent_images), dfp.parent_images


def compute_dockerfile_digest(dockerfile_path):
    with dockerfile_path.open(mode='r') as handle:
        # Read in and standardize linefeed
        content = '\n'.join(handle.read().splitlines())
        m = hashlib.md5()
        m.update(content.encode('utf-8'))
        return m.hexdigest()


def resolve_upstream_from(runtime, image_entry):
    """
    :param runtime: The runtime object
    :param image_entry: A builder or from entry. e.g. { 'member': 'openshift-enterprise-base' }  or { 'stream': 'golang; }
    :return: The upstream CI pullspec to which this entry should resolve.
    """
    major = runtime.group_config.vars['MAJOR']
    minor = runtime.group_config.vars['MINOR']

    if image_entry.member:
        target_meta = runtime.resolve_image(image_entry.member, True)
        image_name = target_meta.config.name.split('/')[-1]
        # In release payloads, images are promoted into an imagestream
        # tag name without the ose- prefix.
        image_name = remove_prefix(image_name, 'ose-')

        # e.g. registry.ci.openshift.org/ocp/4.6:base
        return f'registry.ci.openshift.org/ocp/{major}.{minor}:{image_name}'

    if image_entry.image:
        # CI is on its own. We can't give them an image that isn't available outside the firewall.
        return None
    elif image_entry.stream:
        return runtime.resolve_stream(image_entry.stream).upstream_image


def _get_upstream_source(runtime, image_meta):
    """
    Analyzes an image metadata to find the upstream URL and branch associated with its content.
    :param runtime: The runtime object
    :param image_meta: The metadata to inspect
    :return: A tuple containing (url, branch) for the upstream source OR (None, None) if there
            is no upstream source.
    """
    if "git" in image_meta.config.content.source:
        source_repo_url = image_meta.config.content.source.git.url
        source_repo_branch = image_meta.config.content.source.git.branch.target
        branch_check, err = exectools.cmd_assert(f'git ls-remote --heads {source_repo_url} {source_repo_branch}', strip=True, retries=3)
        if not branch_check:
            # Output is empty if branch does not exist
            source_repo_branch = image_meta.config.content.source.git.branch.fallback
            if source_repo_branch is Missing:
                raise IOError(f'Unable to detect source repository branch for {image_meta.distgit_key}')
    elif "alias" in image_meta.config.content.source:
        alias = image_meta.config.content.source.alias
        if alias not in runtime.group_config.sources:
            raise IOError(f'Unable to find source alias {alias} for {image_meta.distgit_key}')
        source_repo_url = runtime.group_config.sources[alias].url
        source_repo_branch = runtime.group_config.sources[alias].branch.target
    else:
        # No upstream source, no PR to open
        return None, None

    return source_repo_url, source_repo_branch


@images_streams.group("prs", short_help="Manage ART equivalent PRs in upstream.")
def prs():
    pass


@prs.command('open', short_help='Open PRs against upstream component repos that have a FROM that differs from ART metadata.')
@click.option('--github-access-token', metavar='TOKEN', required=True, help='Github access token for user.')
@click.option('--bug', metavar='BZ#', required=False, default=None, help='Title with Bug #: prefix')
@click.option('--interstitial', metavar='SECONDS', default=120, required=False, help='Delay after each new PR to prevent flooding CI')
@click.option('--ignore-ci-master', default=False, is_flag=True, help='Do not consider what is in master branch when determining what branch to target')
@click.option('--draft-prs', default=False, is_flag=True, help='Open PRs as draft PRs')
@click.option('--moist-run', default=False, is_flag=True, help='Do everything except opening the final PRs')
@click.option('--add-auto-labels', default=False, is_flag=True, help='Add auto_labels to PRs; unless running as openshift-bot, you probably lack the privilege to do so')
@click.option('--add-label', default=[], multiple=True, help='Add a label to all open PRs (new and existing) - Requires being openshift-bot')
@pass_runtime
def images_streams_prs(runtime, github_access_token, bug, interstitial, ignore_ci_master, draft_prs, moist_run, add_auto_labels, add_label):
    runtime.initialize(clone_distgits=False, clone_source=False)
    g = Github(login_or_token=github_access_token)
    github_user = g.get_user()

    major = runtime.group_config.vars['MAJOR']
    minor = runtime.group_config.vars['MINOR']
    interstitial = int(interstitial)

    master_major, master_minor = extract_version_fields(what_is_in_master(), at_least=2)
    if not ignore_ci_master and (major > master_major or minor > master_minor):
        # ART building a release before is is in master. Too early to open PRs.
        runtime.logger.warning(f'Target {major}.{minor} has not been in master yet (it is tracking {master_major}.{master_minor}); skipping PRs')
        exit(0)

    prs_in_master = (major == master_major and minor == master_minor) and not ignore_ci_master

    pr_links = {}  # map of distgit_key to PR URLs associated with updates
    new_pr_links = {}
    skipping_dgks = set()  # If a distgit key is skipped, it children will see it in this list and skip themselves.
    for image_meta in runtime.ordered_image_metas():
        dgk = image_meta.distgit_key
        logger = image_meta.logger
        logger.info('Analyzing image')

        alignment_prs_config = image_meta.config.content.source.ci_alignment.streams_prs

        if alignment_prs_config and alignment_prs_config.enabled is not Missing and not alignment_prs_config.enabled:
            # Make sure this is an explicit False. Missing means the default or True.
            logger.info('The image has alignment PRs disabled; ignoring')
            continue

        from_config = image_meta.config['from']
        if not from_config:
            logger.info('Skipping PRs since there is no configured .from')
            continue

        desired_parents = []
        builders = from_config.builder or []
        for builder in builders:
            upstream_image = resolve_upstream_from(runtime, builder)
            if not upstream_image:
                logger.warning(f'Unable to resolve upstream image for: {builder}')
                break
            desired_parents.append(upstream_image)

        parent_upstream_image = resolve_upstream_from(runtime, from_config)
        if len(desired_parents) != len(builders) or not parent_upstream_image:
            logger.warning('Unable to find all ART equivalent upstream images for this image')
            continue

        desired_parents.append(parent_upstream_image)
        desired_parent_digest = calc_parent_digest(desired_parents)
        logger.info(f'Found desired FROM state of: {desired_parents} with digest: {desired_parent_digest}')

        source_repo_url, source_repo_branch = _get_upstream_source(runtime, image_meta)

        if not source_repo_url:
            # No upstream to clone; no PRs to open
            continue

        public_repo_url, public_branch = runtime.get_public_upstream(source_repo_url)
        if not public_branch:
            public_branch = source_repo_branch

        # There are two standard upstream branching styles:
        # release-4.x   : CI fast-forwards from master when appropriate
        # openshift-4.x : Upstream team manages completely.
        # For the former style, we may need to open the PRs against master.
        # For the latter style, always open directly against named branch
        if public_branch.startswith('release-') and prs_in_master:
            # TODO: auto-detect default branch for repo instead of assuming master
            public_branch = 'master'

        _, org, repo_name = split_git_url(public_repo_url)

        public_source_repo = g.get_repo(f'{org}/{repo_name}')

        try:
            fork_repo_name = f'{github_user.login}/{repo_name}'
            fork_repo = g.get_repo(fork_repo_name)
        except UnknownObjectException:
            # Repo doesn't exist; fork it
            fork_repo = github_user.create_fork(public_source_repo)

        fork_branch_name = f'art-consistency-{runtime.group_config.name}-{dgk}'
        fork_branch_head = f'{github_user.login}:{fork_branch_name}'

        fork_branch = None
        try:
            fork_branch = fork_repo.get_branch(fork_branch_name)
        except UnknownObjectException:
            # Doesn't presently exist and will need to be created
            pass
        except GithubException as ge:
            # This API seems to return 404 instead of UnknownObjectException.
            # So allow 404 to pass through as well.
            if ge.status != 404:
                raise

        public_repo_url = convert_remote_git_to_ssh(public_repo_url)
        clone_dir = os.path.join(runtime.working_dir, 'clones', dgk)
        # Clone the private url to make the best possible use of our doozer_cache
        runtime.git_clone(source_repo_url, clone_dir)

        with Dir(clone_dir):
            exectools.cmd_assert(f'git remote add public {public_repo_url}')
            exectools.cmd_assert(f'git remote add fork {convert_remote_git_to_ssh(fork_repo.git_url)}')
            exectools.cmd_assert('git fetch --all', retries=3)

            # The path to the Dockerfile in the target branch
            if image_meta.config.content.source.dockerfile is not Missing:
                # Be aware that this attribute sometimes contains path elements too.
                dockerfile_name = image_meta.config.content.source.dockerfile
            else:
                dockerfile_name = "Dockerfile"

            df_path = Dir.getpath()
            if image_meta.config.content.source.path:
                dockerfile_name = os.path.join(image_meta.config.content.source.path, dockerfile_name)

            df_path = df_path.joinpath(dockerfile_name)

            assignee = None
            try:
                root_owners_path = Dir.getpath().joinpath('OWNERS')
                if root_owners_path.exists():
                    parsed_owners = yaml.load(root_owners_path.read_text())
                    if 'approvers' in parsed_owners:
                        assignee = parsed_owners['approvers'][0]
            except Exception as owners_e:
                yellow_print(f'Error finding assignee in OWNERS for {public_repo_url}: {owners_e}')

            fork_branch_df_digest = None  # digest of dockerfile image names
            if fork_branch:
                # There is already an ART reconciliation branch. Our fork branch might be up-to-date
                # OR behind the times (e.g. if someone commited changes to the upstream Dockerfile).
                # Let's check.

                # If there is already an art reconciliation branch, get an MD5
                # of the FROM images in the Dockerfile in that branch.
                exectools.cmd_assert(f'git checkout fork/{fork_branch_name}')
                fork_branch_df_digest = compute_dockerfile_digest(df_path)

            # Now change over to the target branch in the actual public repo
            exectools.cmd_assert(f'git checkout public/{public_branch}')
            source_branch_parent_digest, source_branch_parents = extract_parent_digest(df_path)
            public_branch_commit, _ = exectools.cmd_assert('git rev-parse HEAD', strip=True)

            print(f'Desired parents: {desired_parents} ({desired_parent_digest})')
            print(f'Source parents: {source_branch_parents} ({source_branch_parent_digest})')
            if desired_parent_digest == source_branch_parent_digest:
                green_print('Desired digest and source digest match; Upstream is in a good state')
                if fork_branch:
                    for pr in list(public_source_repo.get_pulls(state='open', head=fork_branch_head)):
                        if moist_run:
                            yellow_print(f'Would have closed existing PR: {pr.html_url}')
                        else:
                            yellow_print(f'Closing unnecessary PR: {pr.html_url}')
                            pr.edit(status='Closed')
                continue

            cardinality_mismatch = False
            if len(desired_parents) != len(source_branch_parents):
                # The number of FROM statements in the ART metadata does not match the number
                # of FROM statements in the upstream Dockerfile.
                cardinality_mismatch = True

            yellow_print(f'Upstream dockerfile does not match desired state in {public_repo_url}/blob/{public_branch}/{dockerfile_name}')
            print(f'Fork Dockerfile digest: {fork_branch_df_digest}')

            first_commit_line = f"Updating {image_meta.name} builder & base images to be consistent with ART"
            reconcile_info = f"Reconciling with {convert_remote_git_to_https(runtime.gitdata.origin_url)}/tree/{runtime.gitdata.commit_hash}/images/{os.path.basename(image_meta.config_filename)}"

            diff_text = None
            # Three possible states at this point:
            # 1. No fork branch exists
            # 2. It does exist, but is out of date (e.g. metadata has changed or upstream Dockerfile has changed)
            # 3. It does exist, and is exactly how we want it.
            # Let's create a local branch that will contain the Dockerfile in the state we desire.
            work_branch_name = '__mod'
            exectools.cmd_assert(f'git checkout public/{public_branch}')
            exectools.cmd_assert(f'git checkout -b {work_branch_name}')
            with df_path.open(mode='r+') as handle:
                dfp = DockerfileParser(cache_content=True, fileobj=io.BytesIO())
                dfp.content = handle.read()
                handle.truncate(0)
                handle.seek(0)
                if not cardinality_mismatch:
                    dfp.parent_images = desired_parents
                else:
                    handle.writelines([
                        '# URGENT! ART metadata configuration has a different number of FROMs\n',
                        '# than this Dockerfile. ART will be unable to build your component or\n',
                        '# reconcile this Dockerfile until that disparity is addressed.\n'
                    ])
                handle.write(dfp.content)

            diff_text, _ = exectools.cmd_assert(f'git diff {str(df_path)}')

            desired_df_digest = compute_dockerfile_digest(df_path)
            print(f'Desired Dockerfile digest: {desired_df_digest}')

            # Check for any existing open PR
            open_prs = list(public_source_repo.get_pulls(state='open', head=fork_branch_head))

            # A note on why the following `if` cares about whether there is a PR open.
            # We need to check on an edge case.. if the upstream merged our reconciliation PR and THEN proceeded to change
            # their Dockerfile FROMs again, our desired_df_digest will always equal fork_branch_df_digest and
            # we might conclude that we just need to open a PR with our fork branch. However, in this scenario,
            # GitHub will throw an exception like:
            # "No commits between openshift:master and openshift-bot:art-consistency-openshift-4.8-ptp-operator-must-gather"
            # So... to prevent this, if there is no open PR, we should force push to the fork branch to bring it
            # it's commit ahead of the public branch.

            if desired_df_digest != fork_branch_df_digest or not open_prs:
                yellow_print('Found that fork branch is not in sync with public Dockerfile changes')
                if not moist_run:
                    exectools.cmd_assert(f'git add {str(df_path)}')
                    commit_prefix = ''
                    if repo_name.startswith('kubernetes'):
                        # couple repos have this requirement; openshift/kubernetes & openshift/kubernetes-autoscaler.
                        # This check may suffice  for now, but it may eventually need to be in doozer metadata.
                        commit_prefix = 'UPSTREAM: <carry>: '
                    commit_msg = f"""{commit_prefix}{first_commit_line}
{reconcile_info}
"""
                    exectools.cmd_assert(f'git commit -m "{commit_msg}"')  # Add a commit atop the public branch's current state
                    # Create or update the remote fork branch
                    exectools.cmd_assert(f'git push --force fork {work_branch_name}:{fork_branch_name}', retries=3)

            # At this point, we have a fork branch in the proper state
            pr_body = f"""{first_commit_line}
{reconcile_info}

If you have any questions about this pull request, please reach out to `@art-team` in the `#aos-art` coreos slack channel.
"""

            parent_pr_url = None
            parent_meta = image_meta.resolve_parent()
            if parent_meta:
                if parent_meta.distgit_key in skipping_dgks:
                    skipping_dgks.add(image_meta.distgit_key)
                    yellow_print(f'Image has parent {parent_meta.distgit_key} which was skipped; skipping self: {image_meta.distgit_key}')
                    continue

                parent_pr_url = pr_links.get(parent_meta.distgit_key, None)
                if parent_pr_url:
                    if parent_meta.config.content.source.ci_alignment.streams_prs.merge_first:
                        skipping_dgks.add(image_meta.distgit_key)
                        yellow_print(f'Image has parent {parent_meta.distgit_key} open PR ({parent_pr_url}) and streams_prs.merge_first==True; skipping PR opening for this image {image_meta.distgit_key}')
                        continue

                    # If the parent has an open PR associated with it, make sure the
                    # child PR notes that the parent PR should merge first.
                    pr_body += f'\nDepends on {parent_pr_url} . Allow it to merge and then run `/test all` on this PR.'

            if open_prs:
                existing_pr = open_prs[0]
                # Update body, but never title; The upstream team may need set something like a Bug XXXX: there.
                # Don't muck with it.

                try:
                    if alignment_prs_config.auto_label and add_auto_labels:
                        # If we are to automatically add labels to this upstream PR, do so.
                        existing_pr.add_to_labels(*alignment_prs_config.auto_label)

                    if add_label:
                        existing_pr.add_to_labels(*add_label)
                except GithubException as pr_e:
                    # We are not admin on all repos
                    yellow_print(f'Unable to add labels to {existing_pr.html_url}: {str(pr_e)}')

                pr_url = existing_pr.html_url
                pr_links[dgk] = pr_url

                # The pr_body may change and the base branch may change (i.e. at branch cut,
                # a version 4.6 in master starts being tracked in release-4.6 and master tracks
                # 4.7.
                if moist_run:
                    if existing_pr.base.sha != public_branch_commit:
                        yellow_print(f'Would have changed PR {pr_url} to use base {public_branch} ({public_branch_commit}) vs existing {existing_pr.base.sha}')
                    if existing_pr.body != pr_body:
                        yellow_print(f'Would have changed PR {pr_url} to use body "{pr_body}" vs existing "{existing_pr.body}"')
                    if not existing_pr.assignees and assignee:
                        yellow_print(f'Would have changed PR assignee to {assignee}')
                else:
                    if not existing_pr.assignees and assignee:
                        try:
                            existing_pr.add_to_assignees(assignee)
                        except Exception as access_issue:
                            # openshift-bot does not have admin on *all* repositories; permit this error.
                            yellow_print(f'Unable to set assignee for {pr_url}: {access_issue}')
                    existing_pr.edit(body=pr_body, base=public_branch)
                    yellow_print(f'A PR is already open requesting desired reconciliation with ART: {pr_url}')
                continue

            # Otherwise, we need to create a pull request
            if moist_run:
                pr_links[dgk] = f'MOIST-RUN-PR:{dgk}'
                green_print(f'Would have opened PR against: {public_source_repo.html_url}/blob/{public_branch}/{dockerfile_name}.')
                if parent_pr_url:
                    green_print(f'Would have identified dependency on PR: {parent_pr_url}.')
                if diff_text:
                    yellow_print(diff_text)
                else:
                    yellow_print(f'Fork from which PR would be created ({fork_branch_head}) is populated with desired state.')
            else:
                pr_title = first_commit_line
                if bug:
                    pr_title = f'Bug {bug}: {pr_title}'
                try:
                    new_pr = public_source_repo.create_pull(title=pr_title, body=pr_body, base=public_branch, head=fork_branch_head, draft=draft_prs)
                except GithubException as ge:
                    if 'already exists' in json.dumps(ge.data):
                        # In one execution to date, get_pulls did not find the open PR and the code repeatedly hit this
                        # branch -- attempting to recreate the PR. Everything seems right, but the github api is not
                        # returning it. So catch the error and try to move on.
                        pr_url = f'UnknownPR-{fork_branch_head}'
                        pr_links[dgk] = pr_url
                        yellow_print('Issue attempting to find it, but a PR is already open requesting desired reconciliation with ART')
                        continue
                    raise

                try:
                    if alignment_prs_config.auto_label and add_auto_labels:
                        # If we are to automatically add labels to this upstream PR, do so.
                        new_pr.add_to_labels(*alignment_prs_config.auto_label)

                    if add_label:
                        new_pr.add_to_labels(*add_label)
                except GithubException as pr_e:
                    # We are not admin on all repos
                    yellow_print(f'Unable to add labels to {existing_pr.html_url}: {str(pr_e)}')

                pr_msg = f'A new PR has been opened: {new_pr.html_url}'
                pr_links[dgk] = new_pr.html_url
                new_pr_links[dgk] = new_pr.html_url
                logger.info(pr_msg)
                yellow_print(pr_msg)
                print(f'Sleeping {interstitial} seconds before opening another PR to prevent flooding prow...')
                time.sleep(interstitial)

    if new_pr_links:
        print('Newly opened PRs:')
        print(yaml.safe_dump(new_pr_links))

    if pr_links:
        print('Currently open PRs:')
        print(yaml.safe_dump(pr_links))

    if skipping_dgks:
        print('Some PRs were skipped; Exiting with return code 25 to indicate this')
        exit(25)
