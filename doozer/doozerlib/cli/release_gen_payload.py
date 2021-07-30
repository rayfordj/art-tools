import io
import json
from typing import List, Optional, Tuple, Dict, NamedTuple, Iterable, Set

import click
import yaml
from kobo.rpmlib import parse_nvr

from doozerlib.rhcos import RHCOSBuildInspector
from doozerlib.cli import cli, pass_runtime
from doozerlib.image import ImageMetadata, BrewBuildImageInspector, ArchiveImageInspector
from doozerlib.assembly_inspector import AssemblyInspector
from doozerlib.runtime import Runtime
from doozerlib.util import red_print, go_suffix_for_arch, brew_arch_for_go_arch, isolate_nightly_name_components
from doozerlib.assembly import AssemblyTypes, assembly_basis
from doozerlib import exectools
from doozerlib.model import Model


def default_imagestream_base_name(runtime: Runtime) -> str:
    version = runtime.get_minor_version()
    if runtime.assembly is None or runtime.assembly == 'stream':
        return f'{version}-art-latest'
    else:
        return f'{version}-art-assembly-{runtime.assembly}'


def default_imagestream_namespace_base_name() -> str:
    return "ocp"


def payload_imagestream_name_and_namespace(base_imagestream_name: str, base_namespace: str, brew_arch: str, private: bool) -> Tuple[str, str]:
    """
    :return: Returns the imagestream name and namespace to which images for the specified CPU arch and privacy mode should be synced.
    """
    arch_suffix = go_suffix_for_arch(brew_arch)
    priv_suffix = "-priv" if private else ""
    name = f"{base_imagestream_name}{arch_suffix}{priv_suffix}"
    namespace = f"{base_namespace}{arch_suffix}{priv_suffix}"
    return name, namespace


@cli.command("release:gen-payload", short_help="Generate input files for release mirroring")
@click.option("--is-name", metavar='NAME', required=False,
              help="ImageStream .metadata.name value. For example '4.2-art-latest'")
@click.option("--is-namespace", metavar='NAMESPACE', required=False,
              help="ImageStream .metadata.namespace value. For example 'ocp'")
@click.option("--organization", metavar='ORGANIZATION', required=False, default='openshift-release-dev',
              help="Quay ORGANIZATION to mirror into.\ndefault=openshift-release-dev")
@click.option("--repository", metavar='REPO', required=False, default='ocp-v4.0-art-dev',
              help="Quay REPOSITORY in ORGANIZATION to mirror into.\ndefault=ocp-v4.0-art-dev")
@click.option("--exclude-arch", metavar='ARCH', required=False, multiple=True,
              help="Architecture (brew nomenclature) to exclude from payload generation")
@click.option('--permit-mismatched-siblings', default=False, is_flag=True, help='Ignore sibling images building from different commits')
@click.option('--permit-invalid-reference-releases', default=False, is_flag=True, help='Ignore if reference nightlies do not reflect current assembly state. Do not use outside of testing.')
@pass_runtime
def release_gen_payload(runtime: Runtime, is_name: Optional[str], is_namespace: Optional[str], organization: Optional[str], repository: Optional[str], exclude_arch: Tuple[str, ...], permit_mismatched_siblings: bool, permit_invalid_reference_releases: bool):
    """Generates two sets of input files for `oc` commands to mirror
content and update image streams. Files are generated for each arch
defined in ocp-build-data for a version, as well as a final file for
manifest-lists.

One set of files are SRC=DEST mirroring definitions for 'oc image
mirror'. They define what source images we will sync to which
destination repos, and what the mirrored images will be labeled as.

The other set of files are YAML image stream tags for 'oc
apply'. Those are applied to an openshift cluster to define "release
streams". When they are applied the release controller notices the
update and begins generating a new payload with the images tagged in
the image stream.

For automation purposes this command generates a mirroring yaml files
after the arch-specific files have been generated. The yaml files
include names of generated content.

You may provide the namespace and base name for the image streams, or defaults
will be used. The generated files will append the -arch and -priv suffixes to
the given name and namespace as needed.

The ORGANIZATION and REPOSITORY options are combined into
ORGANIZATION/REPOSITORY when preparing for mirroring.

Generate files for mirroring from registry-proxy (OSBS storage) to our
quay registry:

\b
    $ doozer --group=openshift-4.2 release:gen-payload \\
        --is-name=4.2-art-latest

Note that if you use -i to include specific images, you should also include
openshift-enterprise-cli to satisfy any need for the 'cli' tag. The cli image
is used automatically as a stand-in for images when an arch does not build
that particular tag.

## Validation ##

Additionally we want to check that the following conditions are true for each
imagestream being updated:

* For all architectures built, RHCOS builds must have matching versions of any
  unshipped RPM they include (per-entry os metadata - the set of RPMs may differ
  between arches, but versions should not).
* Any RPMs present in images (including machine-os-content) from unshipped RPM
  builds included in one of our candidate tags must exactly version-match the
  latest RPM builds in those candidate tags (ONLY; we never flag what we don't
  directly ship.)

These checks (and likely more in the future) should run and any failures should
be listed in brief via a "release.openshift.io/inconsistency" annotation on the
relevant image istag (these are publicly visible; ref. https://bit.ly/37cseC1)
and in more detail in state.yaml. The release-controller, per ART-2195, will
read and propagate/expose this annotation in its display of the release image.
    """
    runtime.initialize(mode='both', clone_distgits=False, clone_source=False, prevent_cloning=True)
    logger = runtime.logger
    brew_session = runtime.build_retrying_koji_client()

    base_imagestream_name: str = is_name if is_name else default_imagestream_base_name(runtime)
    base_istream_namespace: str = is_namespace if is_namespace else default_imagestream_namespace_base_name()

    if runtime.assembly and runtime.assembly != 'stream' and 'art-latest' in base_imagestream_name:
        raise ValueError('The art-latest imagestreams should not be used for an assembly other than "stream"')

    logger.info(f'Collecting latest information associated with the assembly: {runtime.assembly}')
    assembly_inspector = AssemblyInspector(runtime, brew_session)
    logger.info(f'Checking for mismatched siblings...')
    mismatched_siblings = PayloadGenerator.find_mismatched_siblings(assembly_inspector.get_group_release_images().values())

    # A list of strings that denote inconsistencies across all payloads generated
    assembly_wide_inconsistencies: List[str] = list()

    if runtime.assembly != 'test' and mismatched_siblings and runtime.assembly_type in (AssemblyTypes.STREAM, AssemblyTypes.STANDARD):
        msg = f'At least one set of image siblings were built from the same repo but different commits ({mismatched_siblings}). This is not permitted for {runtime.assembly_type} assemblies'
        if permit_mismatched_siblings:
            red_print(msg)
            assembly_wide_inconsistencies.append(f"Mismatched siblings: {mismatched_siblings}")
        else:
            raise ValueError(msg)

    report = dict()
    report['non_release_images'] = [image_meta.distgit_key for image_meta in runtime.get_non_release_image_metas()]
    report['release_images'] = [image_meta.distgit_key for image_meta in runtime.get_for_release_image_metas()]
    report['mismatched_siblings'] = [build_image_inspector.get_nvr() for build_image_inspector in mismatched_siblings]
    report['missing_image_builds'] = [dgk for (dgk, ii) in assembly_inspector.get_group_release_images().items() if ii is None]  # A list of metas where the assembly did not find a build
    payload_entry_inconsistencies: Dict[str, List[str]] = dict()  # Payload tag -> list of issue strings
    report['payload_entry_inconsistencies'] = payload_entry_inconsistencies
    report['assembly_issues'] = assembly_wide_inconsistencies  # Any overall gripes with the payload

    if runtime.assembly_type is AssemblyTypes.STREAM:
        # Only nightlies have the concept of private and public payloads
        privacy_modes = [False, True]
    else:
        privacy_modes = [False]

    # Structure to record rhcos builds we use so that they can be analyzed for inconsistencies
    targeted_rhcos_builds: Dict[bool, List[RHCOSBuildInspector]] = {
        False: [],
        True: []
    }

    """
    We will be using the shared build status detector in runtime for these images. Warm up its cache.
    """
    build_ids: Set[int] = set()
    for bbii in assembly_inspector.get_group_release_images().values():
        if bbii:
            build_ids.add(bbii.get_brew_build_id())
    with runtime.shared_build_status_detector() as bsd:
        bsd.populate_archive_lists(build_ids)
        bsd.find_shipped_builds(build_ids)

    """
    Make sure that RPMs belonging to this assembly/group are consistent with the assembly definition.
    """
    rpm_meta_inconsistencies: Dict[str, List[str]] = dict()
    for rpm_meta in runtime.rpm_metas():
        issues = assembly_inspector.check_group_rpm_package_consistency(rpm_meta)
        if issues:
            rpm_meta_inconsistencies.get(rpm_meta.distgit_key, []).extend(issues)

    image_meta_inconsistencies: Dict[str, List[str]] = dict()

    """
    If this is a stream assembly, images which are not using the latest builds should not reach
    the release controller.
    """
    if runtime.assembly == 'stream':
        for dgk, build_inspector in assembly_inspector.get_group_release_images().items():
            if build_inspector:
                non_latest_rpm_nvrs = build_inspector.find_non_latest_rpms()
                if non_latest_rpm_nvrs:
                    # This indicates an issue with scan-sources
                    dgk = build_inspector.get_image_meta().distgit_key
                    image_meta_inconsistencies.get(dgk, []).append(f'Found outdated RPMs installed in {build_inspector.get_nvr()}: {non_latest_rpm_nvrs}')

    """
    Make sure image build selected by this assembly/group are consistent with the assembly definition.
    """
    for dgk, bbii in assembly_inspector.get_group_release_images().items():
        if bbii:
            issues = assembly_inspector.check_group_image_consistency(bbii)
            if issues:
                image_meta_inconsistencies.get(dgk, []).extend(issues)

    if image_meta_inconsistencies or rpm_meta_inconsistencies:
        rebuilds_required = {
            'images': image_meta_inconsistencies,
            'rpms': rpm_meta_inconsistencies,
        }
        red_print(f'Unable to proceed. Builds selected by this assembly/group and not compliant with the assembly definition: {assembly_inspector.get_assembly_name()}')
        print(yaml.dump(rebuilds_required, default_flow_style=False, indent=2))
        exit(1)

    for arch in runtime.arches:
        if arch in exclude_arch:
            logger.info(f'Excluding payload files architecture: {arch}')
            continue

        # Whether private or public, the assembly's canonical payload content is the same.
        entries: Dict[str, PayloadGenerator.PayloadEntry] = PayloadGenerator.find_payload_entries(assembly_inspector, arch, f'quay.io/{organization}/{repository}')  # Key of this dict is release payload tag name

        for tag, payload_entry in entries.items():
            if payload_entry.image_meta:
                # We already cached inconsistencies for each build; look them up if there are any.
                payload_entry.inconsistencies.extend(image_meta_inconsistencies.get(payload_entry.image_meta.distgit_key, []))
            elif payload_entry.rhcos_build:
                payload_entry.inconsistencies.extend(assembly_inspector.check_rhcos_consistency(payload_entry.rhcos_build))
                if runtime.assembly == 'stream':
                    # For stream alone, we want to enforce that the very latest RPMs are installed.
                    non_latest_rpm_nvrs = payload_entry.rhcos_build.find_non_latest_rpms()
                    if non_latest_rpm_nvrs:
                        # Raise an error, because this indicates an issue with config:scan-sources.
                        raise IOError(f'Found outdated RPMs installed in {payload_entry.rhcos_build}: {non_latest_rpm_nvrs}; this will likely self correct once the next RHCOS build takes place.')
            else:
                raise IOError(f'Unsupported PayloadEntry: {payload_entry}')
            # Report any inconsistencies to in the final yaml output
            if payload_entry.inconsistencies:
                payload_entry_inconsistencies[tag] = payload_entry.inconsistencies

        # Save the default SRC=DEST input to a file for syncing by 'oc image mirror'. Why is
        # there no '-priv'? The true images for the assembly are what we are syncing -
        # it is what we update in the imagestream that defines whether the image will be
        # part of a public release.
        with io.open(f"src_dest.{arch}", "w+", encoding="utf-8") as out_file:
            for payload_entry in entries.values():
                if not payload_entry.archive_inspector:
                    # Nothing to mirror (e.g. machine-os-content)
                    continue
                out_file.write(f"{payload_entry.archive_inspector.get_archive_pullspec()}={payload_entry.dest_pullspec}\n")

        for private_mode in privacy_modes:
            logger.info(f'Building payload files for architecture: {arch}; private: {private_mode}')

            file_suffix = arch + '-priv' if private_mode else arch
            with io.open(f"image_stream.{file_suffix}.yaml", "w+", encoding="utf-8") as out_file:
                istags: List[Dict] = []
                for payload_tag_name, payload_entry in entries.items():
                    if payload_entry.build_inspector and payload_entry.build_inspector.is_under_embargo() and private_mode is False:
                        # Don't send this istag update to the public release controller
                        continue
                    istags.append(PayloadGenerator.build_payload_istag(payload_tag_name, payload_entry))

                imagestream_name, imagestream_namespace = payload_imagestream_name_and_namespace(
                    base_imagestream_name,
                    base_istream_namespace,
                    arch, private_mode)

                istream_spec = PayloadGenerator.build_payload_imagestream(imagestream_name, imagestream_namespace, istags, assembly_wide_inconsistencies)
                yaml.safe_dump(istream_spec, out_file, indent=2, default_flow_style=False)

    # Now make sure that all of the RHCOS builds contain consistent RPMs
    for private_mode in privacy_modes:
        rhcos_builds = targeted_rhcos_builds[private_mode]
        rhcos_inconsistencies: Dict[str, List[str]] = PayloadGenerator.find_rhcos_build_rpm_inconsistencies(rhcos_builds)
        if rhcos_inconsistencies:
            red_print(f'Found RHCOS inconsistencies in builds {targeted_rhcos_builds}: {rhcos_inconsistencies}')
            raise IOError(f'Found RHCOS inconsistencies in builds')

    # If the assembly claims to have reference nightlies, assert that our payload
    # matches them exactly.
    nightly_match_issues = PayloadGenerator.check_nightlies_consistency(assembly_inspector)
    if nightly_match_issues:
        msg = 'Nightlies in reference-releases did not match constructed payload:\n' + yaml.dump(nightly_match_issues)
        if permit_invalid_reference_releases:
            red_print(msg)
            assembly_wide_inconsistencies.extend(nightly_match_issues)
        else:
            # Artist must remove nightly references or fix the assembly definition.
            raise IOError(msg)

    print(yaml.dump(report, default_flow_style=False, indent=2))


class PayloadGenerator:

    class PayloadEntry(NamedTuple):

        # The destination pullspec
        dest_pullspec: str

        # Append any inconsistencies found for the entry
        inconsistencies: List[str]

        """
        If the entry is for an image in this doozer group, these values will be set.
        """
        # The image metadata which associated with the payload
        image_meta: Optional[ImageMetadata] = None
        # An inspector associated with the overall brew build (manifest list) found for the release
        build_inspector: Optional[BrewBuildImageInspector] = None
        # The brew build archive (arch specific image) that should be tagged into the payload
        archive_inspector: Optional[ArchiveImageInspector] = None

        """
        If the entry is for machine-os-content, this value will be set
        """
        rhcos_build: Optional[RHCOSBuildInspector] = None

    @staticmethod
    def find_mismatched_siblings(build_image_inspectors: Iterable[Optional[BrewBuildImageInspector]]) -> List[BrewBuildImageInspector]:
        """
        Sibling images are those built from the same repository. We need to throw an error
        if there are sibling built from different commits.
        :return: Returns a list of ImageMetadata which had conflicting upstream commits
        """
        class RepoBuildRecord(NamedTuple):
            build_image_inspector: BrewBuildImageInspector
            source_git_commit: str

        # Maps SOURCE_GIT_URL -> RepoBuildRecord(SOURCE_GIT_COMMIT, DISTGIT_KEY, NVR). Where the Tuple is the first build
        # encountered claiming it is sourced from the SOURCE_GIT_URL
        repo_builds: Dict[str, RepoBuildRecord] = dict()

        mismatched_siblings: List[BrewBuildImageInspector] = []
        for build_image_inspector in build_image_inspectors:

            if not build_image_inspector:
                # No build for this component at present.
                continue

            source_url = build_image_inspector.get_source_git_url()
            source_git_commit = build_image_inspector.get_source_git_commit()
            if not source_url or not source_git_commit:
                # This is true for distgit only components.
                continue

            potential_conflict: RepoBuildRecord = repo_builds.get(source_url, None)
            if potential_conflict:
                # Another component has build from this repo before. Make
                # sure it built from the same commit.
                if potential_conflict.source_git_commit != source_git_commit:
                    mismatched_siblings.append(potential_conflict.build_image_inspector)
                    mismatched_siblings.append(build_image_inspector)
                    red_print(f"The following NVRs are siblings but built from different commits: {potential_conflict.build_image_inspector.get_nvr()} and {build_image_inspector.get_nvr()}")
            else:
                # No conflict, so this is our first encounter for this repo; add it to our tracking dict.
                repo_builds[source_url] = RepoBuildRecord(build_image_inspector=build_image_inspector, source_git_commit=source_git_commit)

        return mismatched_siblings

    @staticmethod
    def find_rhcos_build_rpm_inconsistencies(rhcos_builds: List[RHCOSBuildInspector]) -> Dict[str, List[str]]:
        """
        Looks through a set of RHCOS builds and finds if any of those builds contains a package version that
        is inconsistent with the same package in another RHCOS build.
        :return: Returns Dict[inconsistent_rpm_name] -> [inconsistent_nvrs, ...]. The Dictionary will be empty
                 if there are no inconsistencies detected.
        """
        rpm_uses: Dict[str, Set[str]] = {}

        for rhcos_build in rhcos_builds:
            for nvr in rhcos_build.get_rpm_nvrs():
                rpm_name = parse_nvr(nvr)['name']
                if rpm_name not in rpm_uses:
                    rpm_uses[rpm_name] = set()
                rpm_uses[rpm_name].add(nvr)

        # Report back rpm name keys which were associated with more than one NVR in the set of RHCOS builds.
        return {rpm_name: nvr_list for rpm_name, nvr_list in rpm_uses.items() if len(nvr_list) > 1}

    @staticmethod
    def get_mirroring_destination(archive_inspector: ArchiveImageInspector, dest_repo: str) -> str:
        """
        :param archive_inspector: The archive to analyze for mirroring.
        :param dest_repo: A pullspec to mirror to, except for the tag. This include registry, organization, and repo.
        :return: Returns the external (quay) image location to which this image should be mirrored in order
                 to be included in an nightly release payload. These tags are meant to leak no information
                 to users watching the quay repo. The image must have a tag or it will be garbage collected.
        """
        tag = archive_inspector.get_archive_digest().replace(":", "-")  # sha256:abcdef -> sha256-abcdef
        return f"{dest_repo}:{tag}"

    @staticmethod
    def find_payload_entries(assembly_inspector: AssemblyInspector, arch: str, dest_repo: str) -> Dict[str, PayloadEntry]:
        """
        Returns a list of images which should be included in the architecture specific release payload.
        This includes images for our group's image metadata as well as machine-os-content.
        :param assembly_inspector: An analyzer for the assembly to generate entries for.
        :param arch: The brew architecture name to create the list for.
        :param dest_repo: The registry/org/repo into which the image should be mirrored.
        :return: Map[payload_tag_name] -> PayloadEntry.
        """
        members: Dict[str, Optional[PayloadGenerator.PayloadEntry]] = dict()  # Maps release payload tag name to the PayloadEntry for the image.
        for payload_tag, archive_inspector in PayloadGenerator.get_group_payload_tag_mapping(assembly_inspector, arch).items():
            if not archive_inspector:
                # There is no build for this payload tag for this CPU arch. This
                # will be filled in later in this method for the final list.
                members[payload_tag] = None
                continue

            members[payload_tag] = PayloadGenerator.PayloadEntry(
                image_meta=archive_inspector.get_image_meta(),
                build_inspector=archive_inspector.get_brew_build_inspector(),
                archive_inspector=archive_inspector,
                dest_pullspec=PayloadGenerator.get_mirroring_destination(archive_inspector, dest_repo),
                inconsistencies=list(),
            )

        # members now contains a complete map of payload tag keys, but some values may be None. This is an
        # indication that the architecture did not have a build of one of our group images.
        # The tricky bit is that all architecture specific release payloads contain the same set of tags
        # or 'oc adm release new' will have trouble assembling it. i.e. an imagestream tag 'X' may not be
        # necessary on s390x, bit we need to populate that tag with something.

        # To do this, we replace missing images with the 'pod' image for the architecture. This should
        # be available for every CPU architecture. As such, we must find pod to proceed.

        pod_entry = members.get('pod', None)
        if not pod_entry:
            raise IOError(f'Unable to find pod image archive for architecture: {arch}; unable to construct payload')

        final_members: Dict[str, PayloadGenerator.PayloadEntry] = dict()
        for tag_name, entry in members.items():
            if entry:
                final_members[tag_name] = entry
            else:
                final_members[tag_name] = pod_entry

        rhcos_build: RHCOSBuildInspector = assembly_inspector.get_rhcos_build(arch)
        final_members['machine-os-content'] = PayloadGenerator.PayloadEntry(
            dest_pullspec=rhcos_build.get_image_pullspec(),
            rhcos_build=rhcos_build,
            inconsistencies=list(),
        )

        # Final members should have all tags populated.
        return final_members

    @staticmethod
    def build_payload_istag(payload_tag_name: str, payload_entry: PayloadEntry) -> Dict:
        """
        :param payload_tag_name: The name of the payload tag for which to create an istag.
        :param payload_entry: The payload entry to serialize into an imagestreamtag.
        :return: Returns a imagestreamtag dict for a release payload imagestream.
        """
        return {
            'annotations': PayloadGenerator._build_inconsistency_annotation(payload_entry.inconsistencies),
            'name': payload_tag_name,
            'from': {
                'kind': 'DockerImage',
                'name': payload_entry.dest_pullspec,
            }
        }

    @staticmethod
    def build_payload_imagestream(imagestream_name: str, imagestream_namespace: str, payload_istags: Iterable[Dict], assembly_wide_inconsistencies: Iterable[str]) -> Dict:
        """
        Builds a definition for a release payload imagestream from a set of payload istags.
        :param imagestream_name: The name of the imagstream to generate.
        :param imagestream_namespace: The nemspace in which the imagestream should be created.
        :param payload_istags: A list of istags generated by build_payload_istag.
        :param assembly_wide_inconsistencies: Any inconsistency information to embed in the imagestream.
        :return: Returns a definition for an imagestream for the release payload.
        """

        istream_obj = {
            'kind': 'ImageStream',
            'apiVersion': 'image.openshift.io/v1',
            'metadata': {
                'name': imagestream_name,
                'namespace': imagestream_namespace,
                'annotations': PayloadGenerator._build_inconsistency_annotation(assembly_wide_inconsistencies)
            },
            'spec': {
                'tags': list(payload_istags),
            }
        }

        return istream_obj

    @staticmethod
    def _build_inconsistency_annotation(inconsistencies: Iterable[str]):
        """
        :param inconsistencies: A list of strings to report as inconsistencies within an annotation.
        :return: Returns a dict containing an inconsistency annotation out of the specified str.
                 Returns emtpy {} if there are no inconsistencies in the parameter.
        """
        # given a list of strings, build the annotation for inconsistencies
        if not inconsistencies:
            return {}

        inconsistencies = sorted(inconsistencies)
        if len(inconsistencies) > 5:
            # an exhaustive list of the problems may be too large; that goes in the state file.
            inconsistencies[5:] = ["(...and more)"]
        return {"release.openshift.io/inconsistency": json.dumps(inconsistencies)}

    @staticmethod
    def get_group_payload_tag_mapping(assembly_inspector: AssemblyInspector, arch: str) -> Dict[str, Optional[ArchiveImageInspector]]:
        """
        Each payload tag name used to map exactly to one release imagemeta. With the advent of '-alt' images,
        we need some logic to determine which images map to which payload tags for a given architecture.
        :return: Returns a map[payload_tag_name] -> ArchiveImageInspector containing an image for the payload. The value may be
                 None if there is no arch specific build for the tag. This does not include machine-os-content since that
                 is not a member of the group.
        """
        brew_arch = brew_arch_for_go_arch(arch)  # Make certain this is brew arch nomenclature
        members: Dict[str, Optional[ArchiveImageInspector]] = dict()  # Maps release payload tag name to the archive which should populate it
        for dgk, build_inspector in assembly_inspector.get_group_release_images().items():

            if build_inspector is None:
                # There was no build for this image found associated with the assembly.
                # In this case, don't put the tag_name into the imagestream. This is not good,
                # so be verbose.
                red_print(f'Unable to find build for {dgk} for {assembly_inspector.get_assembly_name()}')
                continue

            image_meta: ImageMetadata = assembly_inspector.runtime.image_map[dgk]

            if not image_meta.is_payload:
                # Nothing to do for images which are not in the payload
                continue

            tag_name, explicit = image_meta.get_payload_tag_info()  # The tag that will be used in the imagestreams and whether it was explicitly declared.

            if arch not in image_meta.get_arches():
                # If this image is not meant for this architecture
                members[tag_name] = None  # We still need a placeholder in the tag mapping
                continue

            if tag_name in members and not explicit:
                # If we have already found an entry, there is a precedence we honor for
                # "-alt" images. Specifically, if a imagemeta declares its payload tag
                # name explicitly, it will take precedence over any other entries
                # https://issues.redhat.com/browse/ART-2823
                # This was tag not explicitly declared, so ignore the duplicate image.
                continue

            archive_inspector = build_inspector.get_image_archive_inspector(brew_arch)

            if not archive_inspector:
                # This is no build for this CPU architecture for this build. Don't worry yet,
                # it may be carried by an -alt image or not at all for non-x86 arches.
                members[tag_name] = None
                continue

            members[tag_name] = archive_inspector

        return members

    @staticmethod
    def _check_nightly_consistency(assembly_inspector: AssemblyInspector, nightly: str, arch: str) -> List[str]:
        runtime = assembly_inspector.runtime

        def terminal_issue(msg: str):
            return [msg]

        issues: List[str]
        runtime.logger.info(f'Processing nightly: {nightly}')
        major_minor, brew_cpu_arch, priv = isolate_nightly_name_components(nightly)

        if major_minor != runtime.get_minor_version():
            return terminal_issue(f'Specified nightly {nightly} does not match group major.minor')

        rc_suffix = go_suffix_for_arch(brew_cpu_arch, priv)

        retries: int = 3
        release_json_str = ''
        rc = -1
        pullspec = f'registry.ci.openshift.org/ocp{rc_suffix}/release{rc_suffix}:{nightly}'
        while retries > 0:
            rc, release_json_str, err = exectools.cmd_gather(f'oc adm release info {pullspec} -o=json')
            if rc == 0:
                break
            runtime.logger.warn(f'Error accessing nightly release info for {pullspec}:  {err}')
            retries -= 1

        if rc != 0:
            return terminal_issue(f'Unable to gather nightly release info details: {pullspec}; garbage collected?')

        release_info = Model(dict_to_model=json.loads(release_json_str))
        if not release_info.references.spec.tags:
            return terminal_issue(f'Could not find tags in nightly {nightly}')

        issues: List[str] = list()
        payload_entries: Dict[str, PayloadGenerator.PayloadEntry] = PayloadGenerator.find_payload_entries(assembly_inspector, arch, '')
        for component_tag in release_info.references.spec.tags:  # For each tag in the imagestream
            payload_tag_name: str = component_tag.name  # e.g. "aws-ebs-csi-driver"
            payload_tag_pullspec: str = component_tag['from'].name  # quay pullspec
            if '@' not in payload_tag_pullspec:
                # This speaks to an invalid nightly, so raise and exception
                raise IOError(f'Expected pullspec in {nightly}:{payload_tag_name} to be sha digest but found invalid: {payload_tag_pullspec}')

            pullspec_sha = payload_tag_pullspec.rsplit('@', 1)[-1]
            entry = payload_entries.get(payload_tag_name, None)

            if not entry:
                raise IOError(f'Did not find {nightly} payload tag {payload_tag_name} in computed assembly payload')

            if entry.archive_inspector:
                if entry.archive_inspector.get_archive_digest() != pullspec_sha:
                    issues.append(f'{nightly} contains {payload_tag_name} sha {pullspec_sha} but assembly computed archive: {entry.archive_inspector.get_archive_id()} and {entry.archive_inspector.get_archive_pullspec()}')
            elif entry.rhcos_build:
                if entry.rhcos_build.get_machine_os_content_digest() != pullspec_sha:
                    issues.append(f'{nightly} contains {payload_tag_name} sha {pullspec_sha} but assembly computed rhcos: {entry.rhcos_build} and {entry.rhcos_build.get_machine_os_content_digest()}')
            else:
                raise IOError(f'Unsupported payload entry {entry}')

        return issues

    @staticmethod
    def check_nightlies_consistency(assembly_inspector: AssemblyInspector) -> List[str]:
        """
        If this assembly has reference-releases, check whether the current images selected by the
        assembly are an exact match for the nightly contents.
        """
        basis = assembly_basis(assembly_inspector.runtime.get_releases_config(), assembly_inspector.runtime.assembly)
        if not basis or not basis.reference_releases:
            return []

        issues: List[str] = []
        for arch, nightly in basis.reference_releases.primitive().items():
            issues.extend(PayloadGenerator._check_nightly_consistency(assembly_inspector, nightly, arch))

        return issues
