import os
import re
import lxml.html
from lxml.html.clean import Cleaner
import ghdiff
import gittle.utils
import yaml
from gittle import Gittle
from dulwich.repo import NotGitRepository
from werkzeug.utils import escape, unescape
from realms.lib.util import to_canonical
from realms import cache


class MyGittle(Gittle):

    def file_history(self, path):
        """Returns all commits where given file was modified
        """
        versions = []
        commits_info = self.commit_info()
        seen_shas = set()

        for commit in commits_info:
            try:
                files = self.get_commit_files(commit['sha'], paths=[path])
                file_path, file_data = files.items()[0]
            except IndexError:
                continue

            file_sha = file_data['sha']

            if file_sha in seen_shas:
                continue
            else:
                seen_shas.add(file_sha)

            versions.append(dict(author=commit['author']['name'],
                                 time=commit['time'],
                                 file_sha=file_sha,
                                 sha=commit['sha'],
                                 message=commit['message']))
        return versions

    def mv_fs(self, file_pair):
        old_name, new_name = file_pair
        os.rename(self.path + "/" + old_name, self.path + "/" + new_name)


class Wiki():
    path = None
    base_path = '/'
    default_ref = 'master'
    default_committer_name = 'Anon'
    default_committer_email = 'anon@anon.anon'
    index_page = 'home'
    repo = None

    def __init__(self, path):
        try:
            self.repo = MyGittle(path)
        except NotGitRepository:
            self.repo = MyGittle.init(path)

        self.path = path

    def __repr__(self):
        return "Wiki: %s" % self.path

    def revert_page(self, name, commit_sha, message, username):
        page = self.get_page(name, commit_sha)
        if not page:
            # Page not found
            return None
        commit_info = gittle.utils.git.commit_info(self.repo[commit_sha.encode('latin-1')])
        message = commit_info['message']
        return self.write_page(name, page['data'], message=message, username=username)

    def write_page(self, name, content, message=None, create=False, username=None, email=None):

        def escape_repl(m):
            if m.group(1):
                return "```" + escape(m.group(1)) + "```"

        def unescape_repl(m):
            if m.group(1):
                return "```" + unescape(m.group(1)) + "```"

        cname = to_canonical(name)

        # prevents p tag from being added, we remove this later
        content = '<div>' + content + '</div>'
        content = re.sub(r"```(.*?)```", escape_repl, content, flags=re.DOTALL)

        tree = lxml.html.fromstring(content)

        cleaner = Cleaner(remove_unknown_tags=False,
                          kill_tags=set(['style']),
                          safe_attrs_only=False)
        tree = cleaner.clean_html(tree)

        content = lxml.html.tostring(tree, encoding='utf-8', method='html')

        # remove added div tags
        content = content[5:-6]

        # FIXME this is for block quotes, doesn't work for double ">"
        content = re.sub(r"(\n&gt;)", "\n>", content)
        content = re.sub(r"(^&gt;)", ">", content)

        # Handlebars partial ">"
        content = re.sub(r"\{\{&gt;(.*?)\}\}", r'{{>\1}}', content)

        # Handlebars, allow {{}} inside HTML links
        content = content.replace("%7B", "{")
        content = content.replace("%7D", "}")

        content = re.sub(r"```(.*?)```", unescape_repl, content, flags=re.DOTALL)

        filename = self.cname_to_filename(cname)
        with open(self.path + "/" + filename, 'w') as f:
            f.write(content)

        if create:
            self.repo.add(filename)

        if not message:
            message = "Updated %s" % name

        if not username:
            username = self.default_committer_name

        if not email:
            email = self.default_committer_email

        ret = self.repo.commit(name=username,
                               email=email,
                               message=message,
                               files=[filename])

        cache.delete(cname)

        return ret

    def rename_page(self, old_name, new_name):
        old_name, new_name = map(self.cname_to_filename, [old_name, new_name])
        self.repo.mv([(old_name, new_name)])
        self.repo.commit(name=self.default_committer_name,
                         email=self.default_committer_email,
                         message="Moving %s to %s" % (old_name, new_name),
                         files=[old_name])
        cache.delete_many(old_name, new_name)

    def get_page(self, name, sha='HEAD'):
        cached = cache.get(name)
        if cached:
            return cached

        # commit = gittle.utils.git.commit_info(self.repo[sha])
        name = self.cname_to_filename(name).encode('latin-1')
        sha = sha.encode('latin-1')

        try:
            data = self.repo.get_commit_files(sha, paths=[name]).get(name)
            if not data:
                return None
            partials = {}
            if data.get('data'):
                meta = self.get_meta(data['data'])
                if meta and 'import' in meta:
                    for partial_name in meta['import']:
                        partials[partial_name] = self.get_page(partial_name)
            data['partials'] = partials
            return data

        except KeyError:
            # HEAD doesn't exist yet
            return None

    def get_meta(self, content):
        if not content.startswith("---"):
            return None
        meta_end = re.search("\n(\.{3}|\-{3})", content)
        if not meta_end:
            return None
        try:
            return yaml.safe_load(content[0:meta_end.start()])
        except Exception as e:
            return {'error': e.message}

    def compare(self, name, old_sha, new_sha):
        old = self.get_page(name, sha=old_sha)
        new = self.get_page(name, sha=new_sha)
        return ghdiff.diff(old['data'], new['data'])

    def get_history(self, name):
        return self.repo.file_history(self.cname_to_filename(name))

    @staticmethod
    def cname_to_filename(cname):
        return cname.lower() + ".md"