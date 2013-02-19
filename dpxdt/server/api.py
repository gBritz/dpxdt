#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Web-based API for managing screenshots and incremental diffs.

Lifecycle of a release:

1. User creates a new build, which represents a single product or site that
   will be screenshotted repeatedly over time. This may happen very
   infrequenty through a web UI.

2. User creates a new release candidate for the build with a specific release
   name. The candidate is an attempt at finishing a specific release name. It
   may take many attempts, many candidates, before the release with that name
   is complete and can be marked as good.

3. User creates many runs for the candidate created in #2. Each run is
   identified by a unique name that describes what it does. For example, the
   run name could be the URL path for a page being screenshotted. The user
   associates each run with a new screenshot artifact. Runs are automatically
   associated with a corresponding run from the last good release. This makes
   it easy to compare new and old screenshots for runs with the same name.

4. User uploads a series of screenshot artifacts identified by content hash.
   Perceptual diffs between these new screenshots and the last good release
   may also be uploaded as an optimization. This may happen in parallel
   with #3.

5. The user marks the release candidate as having all of its expected runs
   present, meaning it will no longer receive new runs. This should only
   happen after all screenshot artifacts have finished uploading.

6. If a run indicates a previous screenshot, but no perceptual diff has
   been made to compare the new and old versions, a worker will do a perceptual
   diff, upload it, and associate it with the run.

7. Once all perceptual diffs for a release candidate's runs are complete,
   the results of the candidate are emailed out to the build's owner.

8. The build owner can go into a web UI, inspect the new/old perceptual diffs,
   and mark certain runs as okay even though the perceptual diff showed a
   difference. For example, a new feature will cause a perceptual diff, but
   should not be treated as a failure.

9. The user decides the release candidate looks correct and marks it as good,
   or the user thinks the candidate looks bad and goes back to #2 and begins
   creating a new candidate for that release all over again.


Notes:

- At any time, a user can manually mark any candidate or release as bad. This
  is useful to deal with bugs in the screenshotter, mistakes in approving a
  release candidate, rolling back to an earlier version, etc.

- As soon as a new release name is cut for a build, the last candidate of
  the last release is marked as good if there is no other good candidate. This
  lets the API establish a "baseline" release easily for first-time users.

- Only one release candidate may be receiving runs for a build at a time.
"""

import datetime
import hashlib
import json
import logging
import mimetypes

# Local libraries
import flask
from flask import Flask, request
from flask.ext.sqlalchemy import SQLAlchemy

# Local modules
import server
app = server.app
db = server.db
import work_queue
import utils


class Build(db.Model):
    """A single repository of artifacts and diffs owned by someone.

    Queries:
    - Get all builds for a specific owner.
    - Can this user read this build.
    - Can this user write this build.
    """

    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    name = db.Column(db.String)
    # TODO: Add owner


class Release(db.Model):
    """A set of runs that are part of a build, grouped by a user-supplied name.

    Queries:
    - For a build, find me the active release with this name.
    - Mark this release as abandoned.
    - Show me all active releases for this build by unique name in order
      of creation date descending.
    """

    RECEIVING = 'receiving'
    PROCESSING = 'processing'
    REVIEWING = 'reviewing'
    BAD = 'bad'
    GOOD = 'good'
    STATES = frozenset([RECEIVING, PROCESSING, REVIEWING, BAD, GOOD])

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    number = db.Column(db.Integer, nullable=False)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    status = db.Column(db.Enum(*STATES), default=RECEIVING, nullable=False)
    build_id = db.Column(db.Integer, db.ForeignKey('build.id'), nullable=False)


class Artifact(db.Model):
    """Contains a single file uploaded by a diff worker."""

    id = db.Column(db.String(40), primary_key=True)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    data = db.Column(db.LargeBinary)
    content_type = db.Column(db.String)


class Run(db.Model):
    """Contains a set of screenshot records uploaded by a diff worker.

    Queries:
    - Show me all runs for the given release.
    - Show me all runs with the given name for all releases that are live.
    """

    id = db.Column(db.Integer, primary_key=True)
    release_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String, nullable=False)

    created = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    image = db.Column(db.String, db.ForeignKey('artifact.id'))
    log = db.Column(db.String, db.ForeignKey('artifact.id'))
    config = db.Column(db.String, db.ForeignKey('artifact.id'))

    previous_id = db.Column(db.Integer, db.ForeignKey('run.id'))

    needs_diff = db.Column(db.Boolean)
    diff_image = db.Column(db.String, db.ForeignKey('artifact.id'))
    diff_log = db.Column(db.String, db.ForeignKey('artifact.id'))


@app.route('/api/build', methods=['POST'])
def create_build():
    """Creates a new build for a user."""
    # TODO: Make sure the requesting user is logged in
    name = request.form.get('name')
    utils.jsonify_assert(name, 'name required')

    build = Build(name=name)
    db.session.add(build)
    db.session.commit()

    logging.info('Created build: build_id=%s, name=%r', build.id, name)

    return flask.jsonify(build_id=build.id, name=name)


@app.route('/api/release', methods=['POST'])
def create_release():
    """Creates a new release candidate for a build."""
    build_id = request.form.get('build_id', type=int)
    utils.jsonify_assert(build_id is not None, 'build_id required')
    name = request.form.get('name')
    utils.jsonify_assert(name, 'name required')
    # TODO: Make sure build_id exists
    # TODO: Make sure requesting user is owner of the build_id

    release = Release(
        name=name,
        number=1,
        build_id=build_id)

    last_candidate = (
        Release.query
        .filter_by(build_id=build_id, name=name)
        .order_by(Release.number.desc())
        .first())
    if last_candidate:
        release.number += last_candidate.number

    db.session.add(release)
    db.session.commit()

    logging.info('Created release: build_id=%s, name=%r, number=%d',
                 build_id, name, release.number)

    return flask.jsonify(build_id=build_id, name=name, number=release.number)


def _check_release_done_processing(release_id):
    """Moves a release candidate to reviewing if all runs are done."""
    release = Release.query.get(release_id)
    if not release:
        logging.error('Could not find release_id=%s', release_id)
        return False

    if release.status != Release.PROCESSING:
        logging.error('Already done processing: release_id=%s', release_id)
        return False

    query = Run.query.filter_by(release_id=release.id)
    for run in query:
        if run.needs_diff:
            return False

    logging.info('Release done processing, now reviewing: build_id=%s, '
                 'name=%s, number=%d', release.build_id, release.name,
                 release.number)

    release.status = Release.REVIEWING
    db.session.add(release)
    return True


def _get_release_params():
    """Gets the release params from the current request."""
    build_id = request.form.get('build_id', type=int)
    utils.jsonify_assert(build_id is not None, 'build_id required')
    name = request.form.get('name')
    utils.jsonify_assert(name, 'name required')
    number = request.form.get('number', type=int)
    utils.jsonify_assert(number is not None, 'number required')
    return build_id, name, number


@app.route('/api/report_run', methods=['POST'])
def report_run():
    """Reports a new run for a release candidate."""
    build_id, name, number = _get_release_params()

    release = (
        Release.query
        .filter_by(build_id=build_id, name=name, number=number)
        .first())
    utils.jsonify_assert(release, 'release does not exist')
    # TODO: Make sure requesting user is owner of the build_id

    current_image = request.form.get('image', type=str)
    utils.jsonify_assert(current_image, 'image must be supplied')
    current_log = request.form.get('log', type=str)
    current_config = request.form.get('config', type=str)
    no_diff = request.form.get('no_diff')
    diff_image = request.form.get('diff_image', type=str)
    diff_log = request.form.get('diff_log', type=str)
    needs_diff = not (no_diff or diff_image or diff_log)

    # Find the previous corresponding run and automatically connect it.
    last_good_release = (
        Release.query
        .filter_by(build_id=build_id, name=name, status=Release.GOOD)
        .order_by(Release.created.desc())
        .first())
    previous_id = None
    if last_good_release:
        last_good_run = (
            Run.query
            .filter_by(release_id=last_good_release.id, name=name)
            .first())
        if last_good_run:
            previous_id = last_good_run.id

    fields = dict(
        name=name,  # xxx This name needs to be something else
        release_id=release.id,
        image=current_image,
        log=current_log,
        config=current_config,
        previous_id=previous_id,
        needs_diff=needs_diff,
        diff_image=diff_image,
        diff_log=diff_log)
    run = Run(**fields)
    db.session.add(run)
    db.session.flush()

    fields.update(run_id=run.id)

    # Schedule pdiff if there isn't already an image.
    if needs_diff:
        work_queue.add('run-pdiff', dict(run_id=run.id))

    db.session.commit()

    logging.info('Created run: build_id=%s, name=%r, number=%d',
                 build_id, name, number)

    return flask.jsonify(**fields)


@app.route('/api/report_pdiff', methods=['POST'])
def report_pdiff():
    """Reports a pdiff for a run.

    When there is no diff to report, supply the "no_diff" parameter.
    """
    run_id = request.form.get('run_id', type=int)
    utils.jsonify_assert(run_id is not None, 'run_id required')
    no_diff = request.form.get('no_diff')

    run = Run.query.get(run_id)
    utils.jsonify_assert(run, 'Run does not exist')

    run.needs_diff = not (no_diff or run.diff_image or run.diff_log)
    run.diff_image = request.form.get('diff_image', type=int)
    run.diff_log = request.form.get('diff_log', type=int)

    db.session.add(run)

    logging.info('Saved pdiff: run_id=%s, no_diff=%s, diff_image=%s, '
                 'diff_log=%s', run_id, no_diff, run.diff_image, run.diff_log)

    _check_release_done_processing(run.release_id)
    db.session.commit()

    return flask.jsonify(success=True)


@app.route('/api/runs_done', methods=['POST'])
def runs_done():
    """Marks a release candidate as having all runs reported."""
    build_id, name, number = _get_release_params()

    release = (
        Release.query
        .filter_by(build_id=build_id, name=name, number=number)
        .first())
    utils.jsonify_assert(release, 'Release does not exist')

    release.status = Release.PROCESSING
    db.session.add(release)
    _check_release_done_processing(release)
    db.session.commit()

    logging.info('Runs done for release: build_id=%s, name=%s, number=%d',
                 build_id, name, number)

    return flask.jsonify(success=True)


@app.route('/api/release_done', methods=['POST'])
def release_done():
    """Marks a release candidate as good or bad."""
    build_id, name, number = _get_release_params()
    status = request.form.get('status')
    valid_statuses = (Release.GOOD, Release.BAD)
    utils.jsonify_assert(status in valid_statuses,
                         'status must be in %r' % valid_statuses)

    release = (
        Release.query
        .filter_by(build_id=build_id, name=name, number=number)
        .first())
    utils.jsonify_assert(release, 'Release does not exist')

    release.status = status
    db.session.add(release)
    db.session.commit()

    logging.info('Release marked as %s: build_id=%s, name=%s, number=%d',
                 status, build_id, name, number)

    return flask.jsonify(success=True)


@app.route('/api/upload', methods=['POST'])
def upload():
    """Uploads an artifact referenced by a run."""
    # TODO: Require an API key on the basic auth header
    utils.jsonify_assert(len(request.files) == 1,
                         'Need exactly one uploaded file')

    file_storage = request.files.values()[0]
    data = file_storage.read()
    sha1sum = hashlib.sha1(data).hexdigest()
    exists = Artifact.query.filter_by(id=sha1sum).first()
    if exists:
        logging.info('Upload already exists: artifact_id=%s', sha1sum)
        return flask.jsonify(sha1sum=sha1sum)

    content_type, _ = mimetypes.guess_type(file_storage.filename)
    artifact = Artifact(
        id=sha1sum,
        content_type=content_type,
        data=data)
    db.session.add(artifact)
    db.session.commit()

    logging.info('Upload received: artifact_id=%s, content_type=%s',
                 sha1sum, content_type)
    return flask.jsonify(sha1sum=sha1sum)
